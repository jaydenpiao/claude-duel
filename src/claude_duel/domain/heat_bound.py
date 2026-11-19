"""Heat comparison rules and the host work bound (ADR 0006 addendum). Pure: the caller counts the work.

``regime(rule)`` names the one rule a log is scored by:

- ``legacy``: no pins (logs from before the caps). Exact whole-source LCS while both walks fit ``TOKEN_CAP``
  and the references fit ``REF_CAP``; past that ``HeatBoundError``, and replay reports
  the round as ``heat legacy-unreplayable``.
- ``prefix``: both caps pinned, compare not ``full``. LCS of the first ``heat_token_cap`` tokens.
- ``full``: new matches. Whole-source LCS: a copied body is decided in linear time, other pairs by the
  bit-parallel LCS inside the call's ``WORK_BUDGET``. The multiset ceiling is only a cold-skip filter (if it
  is below the threshold, the exact LCS is too). Every source under ``PARSE_BYTES`` keeps its full walk, so a
  long exact copy is compared (a copied body in linear time). Admission caps a source so every pair a legal
  round can admit fits in ``WORK_BUDGET`` (``admit``); a pair over the work guard is ``heat over budget``:
  it never raises, is charged 0, and is never scored on a bound (S13-4).

All three go through ``compare``. LCS is exact in every regime, so old logs replay unchanged.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from claude_duel.domain.errors import HeatBoundError
from claude_duel.domain.heat_tokens import REF_CAP, TOKEN_CAP, Profile, profile
from claude_duel.domain.models import LedgerRule, Rules

Regime = Literal["legacy", "prefix", "full"]
MAX_CHALLENGES = 20  # two agents times challenges_per_round's maximum
WORD = 64  # bits per machine word: the work unit of the bit-parallel LCS
WORK_BUDGET = 1_000_000_000  # word operations per heat call (about 6 s); 32k x 32k tokens is 16.8M (~0.1 s)
DENSE = 2  # advertised byte cap is admit//DENSE; a denser walk is still refused at heavy


class Hot(NamedTuple):
    """A heat percent. ``exact`` False: some reference could not be compared, so ``pct`` is the best exact
    similarity found (a lower bound of the true best); it is shown as ``heat over budget`` and charged only when
    that exact value alone reaches the threshold, never on a bound."""

    pct: int
    exact: bool


def regime(rule: LedgerRule) -> Regime:
    """The comparison rule a log is scored by. ``LedgerRule`` validation already refused a partial pin."""
    if rule.heat_token_cap is None:
        return "legacy"
    return "full" if rule.heat_compare == "full" else "prefix"


def pin_heat_bound(rules: Rules) -> Rules:
    """Config rules -> the rules a new log stores: both caps (the ceiling when unset) and ``full``
    (``modes.pin_opponent_heat`` pins the opponent flag). Heat off is unchanged. Only config is passed here; a logged
    contract (a resumed or child log) is never re-pinned."""
    led = rules.ledger
    if led is None or not led.heat_window:
        return rules
    pins = {
        "heat_token_cap": led.heat_token_cap or TOKEN_CAP,
        "heat_ref_cap": led.heat_ref_cap or REF_CAP,
        "heat_compare": "full",
    }
    if all(getattr(led, k) == v for k, v in pins.items()):
        return rules
    return rules.model_copy(update={"ledger": led.model_copy(update=pins)})


def lcs(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Exact LCS length, bit-parallel (Hyyro 2004) over a ``len(b)``-bit int: ``work(a, b)`` word operations."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return 0
    masks: dict[str, int] = {}
    for j, token in enumerate(b):
        masks[token] = masks.get(token, 0) | (1 << j)
    full = (1 << len(b)) - 1
    v = full
    for token in a:
        m = masks.get(token)
        if m:
            u = v & m
            v = ((v + u) | (v - u)) & full
    return len(b) - v.bit_count()


def work(a: int, b: int) -> int:
    """Word operations ``lcs`` spends on walks of ``a`` and ``b`` tokens."""
    return max(a, b) * (min(a, b) // WORD + 1)


def slots(rules: Rules) -> int:
    """Current challenges times the reference cap: every pair one heat call may have to compare."""
    current = min(MAX_CHALLENGES, 2 * rules.challenges_per_round)
    led = rules.ledger
    refs = REF_CAP if led is None or led.heat_ref_cap is None else led.heat_ref_cap
    return max(1, current * max(1, refs))


def admit(rules: Rules) -> int:
    """Longest walk such that ``slots(rules)`` same-size pairs fit in ``WORK_BUDGET``."""
    room = WORK_BUDGET // slots(rules)
    lo, hi = 0, room
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if work(mid, mid) <= room:
            lo = mid
        else:
            hi = mid - 1
    return lo


def admit_bytes(rules: Rules) -> int:
    """Byte cap advertised for full-heat admission; the walk is still checked against ``admit``."""
    return max(1, admit(rules) // DENSE)


def heavy(source: str, rules: Rules) -> str | None:
    """Reason a full-heat source's walk would not fit the call, or None."""
    led = rules.ledger
    if led is None or not led.heat_window or led.heat_compare != "full":
        return None
    walked = profile(source)
    if walked is None:
        return None
    n = admit(rules)
    if walked.length > n:
        return f"source exceeds {n} heat tokens"
    return None


def overlap(left: dict[str, int], right: dict[str, int]) -> int:
    """Multiset intersection size; an upper bound on LCS length. Used only as a cold-skip filter."""
    return sum(min(n, right.get(t, 0)) for t, n in left.items())


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


UNCHECKED = Hot(0, False)


def _exact(left: Profile, right: Profile, mode: Regime, room: int, denom: int) -> tuple[Hot, int]:
    """Exact subsequence or LCS, or unchecked / ``HeatBoundError`` when the work does not fit."""
    ta, tb = left.tokens, right.tokens
    if ta is not None and tb is not None and (_is_subseq(ta, tb) or _is_subseq(tb, ta)):
        return Hot(100 * min(left.length, right.length) // denom, True), 0
    spent = work(left.length, right.length)
    if ta is not None and tb is not None and spent <= room:
        return Hot(100 * lcs(ta, tb) // denom, True), spent
    if mode != "full":
        raise HeatBoundError()  # prefix and legacy pairs are at most TOKEN_CAP square: never here in practice
    return UNCHECKED, 0


def compare(
    a: Profile | None, b: Profile | None, mode: Regime, width: int, room: int, threshold: int
) -> tuple[Hot, int]:
    """``(similarity, work spent)`` for one pair under ``mode``. ``room`` is the call's remaining work.
    ``threshold`` is ``heat_threshold_pct``: a pair whose multiset ceiling is below it is skipped as cold."""
    left, right = _view(a, mode, width), _view(b, mode, width)
    if left is None or right is None:
        if mode == "legacy":
            raise HeatBoundError()
        return UNCHECKED, 0  # over PARSE_BYTES: admission and history keep these out of new matches
    if left.length == 0 or right.length == 0:
        return Hot(0, True), 0
    if mode == "legacy" and max(left.length, right.length) > TOKEN_CAP:
        raise HeatBoundError()
    denom = max(left.length, right.length)
    if 100 * overlap(left.counts, right.counts) // denom < threshold:
        return Hot(0, True), 0  # cold: the exact LCS cannot reach the threshold; never charge the ceiling
    return _exact(left, right, mode, room, denom)
