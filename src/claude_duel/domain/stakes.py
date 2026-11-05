"""Sealed Ledger stakes (design-core-logic.md 4.2 step 4): pure checks on an attacker's allocation.

Stakes are positional: index i is the opponent's i-th challenge of the round in sorted challenge-id order, so an
allocation maps to the same challenges on replay. (Not to be confused with the spend ledger in adapters/store.)
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import TypeGuard

from claude_duel.domain.models import Challenge, LedgerRule, MatchState

__all__ = ["LedgerRule", "even_split", "keystone_commit", "keystone_salt", "keystone_targets", "settle_stakes"]


def even_split(k: int, rule: LedgerRule) -> tuple[int, ...]:
    """The deterministic fallback; always passes ``settle_stakes`` itself.

    ``attack_tokens`` shared evenly (remainder to the first challenges). When an even share would fall below
    ``min_alloc``, the first ``attack_tokens // min_alloc`` challenges get ``min_alloc`` and the rest 0.
    """
    if k <= 0:
        return ()
    q, r = divmod(rule.attack_tokens, k)
    if q >= rule.min_alloc:
        return tuple(q + (i < r) for i in range(k))
    funded = rule.attack_tokens // rule.min_alloc
    return tuple(rule.min_alloc if i < funded else 0 for i in range(k))


def settle_stakes(raw: object, k: int, rule: LedgerRule) -> tuple[tuple[int, ...], bool]:
    """The stakes to use and whether they fell back to ``even_split`` (the arena logs ``alloc_fallback``)."""
    if _valid(raw, k, rule):
        return tuple(raw), False
    return even_split(k, rule), True


def _valid(raw: object, k: int, rule: LedgerRule) -> TypeGuard[Sequence[int]]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or len(raw) != k:
        return False
    if not all(type(s) is int for s in raw):  # bool is not a stake
        return False
    return all(s == 0 or s >= rule.min_alloc for s in raw) and sum(raw) <= rule.attack_tokens


def keystone_salt(seed: str, round_no: int, author: str) -> str:
    """Deterministic from the match seed, so replay rebuilds it; logged only at reveal."""
    return hashlib.sha256(f"{seed}:{round_no}:{author}:keystone".encode()).hexdigest()[:16]


def keystone_commit(index: int | None, salt: str) -> str:
    return hashlib.sha256(f"{'none' if index is None else index}:{salt}".encode()).hexdigest()


def open_keystone(commit: str, salt: str, n: int) -> int | None:
    """The index (or None, forfeited) that ``commit`` seals over ``n`` own challenges; the reveal logs it."""
    candidates: list[int | None] = [*range(n), None]
    for index in candidates:
        if keystone_commit(index, salt) == commit:
            return index
    raise ValueError("keystone commit matches no index")


def own_challenges(challenges: Mapping[str, Challenge], round_no: int, author: str) -> list[Challenge]:
    """An author's challenges of one round in keystone (sorted id) order."""
    return sorted((c for c in challenges.values() if c.round == round_no and c.author == author), key=lambda c: c.id)


def pick_keystone(raw: object, own: Sequence[Challenge], valid: Mapping[str, bool]) -> int | None:
    """The index used, or None (forfeit) for a non-int, out-of-range or invalid-challenge pick."""
    if type(raw) is not int or not 0 <= raw < len(own):
        return None
    return raw if valid.get(own[raw].id, False) else None


def keystone_targets(state: MatchState, round_no: int) -> dict[str, str]:
    """author -> keystone challenge id, for revealed, non-forfeited keystones only (what the scorer sees)."""
    out: dict[str, str] = {}
    for key, ks in state.keystones.items():
        r, author = key.split(":", 1)
        if int(r) == round_no and ks.revealed and ks.index is not None:
            own = own_challenges(state.challenges, round_no, author)
            if ks.index < len(own):
                out[author] = own[ks.index].id
    return out
