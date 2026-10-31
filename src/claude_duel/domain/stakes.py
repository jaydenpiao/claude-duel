"""Sealed Ledger stakes (design-core-logic.md 4.2 step 4): pure checks on an attacker's allocation.

Stakes are positional: index i is the opponent's i-th challenge of the round in sorted challenge-id order, so an
allocation maps to the same challenges on replay. (Not to be confused with the spend ledger in adapters/store.)
"""

from collections.abc import Sequence
from typing import TypeGuard

from claude_duel.domain.models import LedgerRule

__all__ = ["LedgerRule", "even_split", "settle_stakes"]


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
