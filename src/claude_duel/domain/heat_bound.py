"""Heat comparison rules and the host work bound (ADR 0006 addendum). Pure: the caller counts the work.

``regime(rule)`` names the one rule a log is scored by:

- ``legacy``: no pins (logs from before the caps). Exact whole-source LCS while both walks fit ``TOKEN_CAP``
  and the references fit ``REF_CAP``; past that ``HeatBoundError``, and replay reports the round unverified.
- ``prefix``: both caps pinned, compare not ``full``. LCS of the first ``heat_token_cap`` tokens.
- ``full``: new matches. Whole-source LCS; a copied body is decided in linear time, other pairs run the DP
  inside ``PAIR_CELLS`` and the call's ``CELL_BUDGET``. Past that the pair is charged on the multiset ceiling,
  an upper bound marked inexact; it never raises.

All three go through ``compare``.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from claude_duel.domain.errors import HeatBoundError
from claude_duel.domain.heat_tokens import REF_CAP, TOKEN_CAP, Profile
from claude_duel.domain.models import LedgerRule, Rules

Regime = Literal["legacy", "prefix", "full"]
MAX_CHALLENGES = 20  # two agents times challenges_per_round's maximum
CELL_BUDGET = MAX_CHALLENGES * REF_CAP * TOKEN_CAP * TOKEN_CAP  # DP cell updates per heat call
PAIR_CELLS = 4_000_000  # one DP above this (~0.4s) is not run


class Hot(NamedTuple):
    """A heat percent. ``exact`` False: the multiset ceiling used past the work bound, never shown as exact."""

    pct: int
    exact: bool


def regime(rule: LedgerRule) -> Regime:
    """The comparison rule a log is scored by. ``LedgerRule`` validation already refused a partial pin."""
    if rule.heat_token_cap is None:
        return "legacy"
    return "full" if rule.heat_compare == "full" else "prefix"


def pin_heat_bound(rules: Rules) -> Rules:
    """Config rules -> the rules a new log stores: both caps (the ceiling when unset), ``full``, and opponent heat
    (design R5) unless the config turned it off. Heat off is unchanged. Only config is passed here; a logged
    contract (a resumed or child log) is never re-pinned."""
    led = rules.ledger
    if led is None or not led.heat_window:
        return rules
    pins = {
        "heat_token_cap": led.heat_token_cap or TOKEN_CAP,
        "heat_ref_cap": led.heat_ref_cap or REF_CAP,
        "heat_compare": "full",
        "heat_vs_opponent": led.heat_vs_opponent is not False,
    }
    if all(getattr(led, k) == v for k, v in pins.items()):
        return rules
    return rules.model_copy(update={"ledger": led.model_copy(update=pins)})


def lcs(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Exact LCS length; ``len(a) * len(b)`` cell updates. The caller bounds the size."""
    prev = [0] * (len(b) + 1)
    for token in a:
        cur = [0]
        for j, other in enumerate(b):
            cur.append(prev[j] + 1 if token == other else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def _is_subseq(small: tuple[str, ...], big: tuple[str, ...]) -> bool:
    """True when ``small`` occurs in ``big`` in order; then the LCS is ``len(small)``."""
    if not small:
        return True
    index = 0
    for token in big:
        if token == small[index]:
            index += 1
            if index == len(small):
                return True
    return False


def _overlap(left: dict[str, int], right: dict[str, int]) -> int:
    """Multiset intersection size: an upper bound on the LCS."""
    if len(left) > len(right):
        left, right = right, left
    return sum(min(count, right.get(name, 0)) for name, count in left.items())


def _view(walk: Profile | None, mode: Regime, width: int) -> Profile | None:
    """What ``mode`` compares: the whole walk, or its first ``width`` tokens (an unparsed source is empty)."""
    if mode != "prefix":
        return walk
    if walk is None:
        return Profile((), 0, {})
    kept = walk.kept[:width]
    counts: dict[str, int] = {}
    for token in kept:
        counts[token] = counts.get(token, 0) + 1
    return Profile(kept, len(kept), counts)


def compare(
    a: Profile | None, b: Profile | None, mode: Regime, width: int, threshold: int, room: int
) -> tuple[Hot, int]:
    """``(similarity, DP cells spent)`` for one pair under ``mode``. ``room`` is the call's remaining budget."""
    left, right = _view(a, mode, width), _view(b, mode, width)
    if left is None or right is None:
        if mode == "legacy":
            raise HeatBoundError()
        return Hot(100, False), 0  # admission and history keep these out of new matches; the trivial bound
    if left.length == 0 or right.length == 0:
        return Hot(0, True), 0
    if mode == "legacy" and max(left.length, right.length) > TOKEN_CAP:
        raise HeatBoundError()
    denom = max(left.length, right.length)
    ta, tb = left.tokens, right.tokens
    if ta is not None and tb is not None and (_is_subseq(ta, tb) or _is_subseq(tb, ta)):
        return Hot(100 * min(left.length, right.length) // denom, True), 0
    cells = left.length * right.length
    if ta is not None and tb is not None and cells <= min(room, PAIR_CELLS):
        return Hot(100 * lcs(ta, tb) // denom, True), cells
    if mode != "full":
        raise HeatBoundError()  # prefix pairs never get here (width <= TOKEN_CAP); legacy is refused above
    ceiling = 100 * _overlap(left.counts, right.counts) // denom
    return Hot(ceiling, ceiling < threshold), 0
