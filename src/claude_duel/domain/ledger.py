"""Sealed Ledger allocation (design-core-logic.md 4.2 step 4): pure checks on an attacker's stakes."""

from collections.abc import Sequence

from claude_duel.domain.models import LedgerRule

__all__ = ["LedgerRule", "even_split", "settle_stakes"]


def even_split(k: int, rule: LedgerRule) -> tuple[int, ...]:
    """The deterministic fallback: ``attack_tokens`` shared evenly, remainder to the first challenges."""
    q, r = divmod(rule.attack_tokens, k)
    return tuple(q + (i < r) for i in range(k))


def settle_stakes(raw: object, k: int, rule: LedgerRule) -> tuple[tuple[int, ...], bool]:
    """The stakes to use and whether they fell back to ``even_split`` (the arena logs ``alloc_fallback``)."""
    if _valid(raw, k, rule):
        assert isinstance(raw, Sequence)
        return tuple(raw), False
    return even_split(k, rule), True


def _valid(raw: object, k: int, rule: LedgerRule) -> bool:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or len(raw) != k:
        return False
    if not all(type(s) is int for s in raw):  # bool is not a stake
        return False
    return all(s == 0 or s >= rule.min_alloc for s in raw) and sum(raw) <= rule.attack_tokens
