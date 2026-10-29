"""Novelty heat (design-core-logic.md 4.3): a challenge too close to its author's own recent ones defends for half.

Pure and computed from logged source only: the fingerprint is a set of shingles over the normalized AST (node
types in pre-order, names and constants erased), compared by Jaccard in integer percent.
"""

import ast
from collections.abc import Iterable

from claude_duel.domain.models import Challenge, LedgerRule

SHINGLE = 4


def fingerprint(source: str) -> frozenset[tuple[str, ...]]:
    """Shingles of ``SHINGLE`` consecutive node types; renaming or changing literals leaves it unchanged."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return frozenset()
    kinds = [type(n).__name__ for n in ast.walk(tree) if not isinstance(n, (ast.Load, ast.Store, ast.Del))]
    if len(kinds) < SHINGLE:
        return frozenset({tuple(kinds)}) if kinds else frozenset()
    return frozenset(tuple(kinds[i : i + SHINGLE]) for i in range(len(kinds) - SHINGLE + 1))


def similarity(a: str, b: str) -> int:
    """Jaccard of the two fingerprints, integer percent (0 when either is empty)."""
    fa, fb = fingerprint(a), fingerprint(b)
    if not fa or not fb:
        return 0
    return len(fa & fb) * 100 // len(fa | fb)


def heat(challenges: Iterable[Challenge], round_no: int, rule: LedgerRule) -> dict[str, int]:
    """challenge id -> its highest similarity to the author's own challenges of the last ``heat_window`` rounds,
    for this round's challenges at or over ``heat_threshold_pct`` only (those defend for half)."""
    every = [c for c in challenges if c.spec is not None]
    out: dict[str, int] = {}
    for ch in (c for c in every if c.round == round_no):
        assert ch.spec is not None
        past = [
            c.spec.source
            for c in every
            if c.author == ch.author and round_no - rule.heat_window <= c.round < round_no and c.spec is not None
        ]
        top = max((similarity(ch.spec.source, s) for s in past), default=0)
        if rule.heat_window and top >= rule.heat_threshold_pct:
            out[ch.id] = top
    return out
