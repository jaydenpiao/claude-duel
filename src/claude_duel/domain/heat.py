"""Novelty heat (design-core-logic.md 4.3, ADR 0006 addendum): a separate defender-side penalty line.

Computed from logged source only. Similarity is ``100 * LCS // max(len)`` (integer floor) over the AST walks of
``heat_tokens``. A defended challenge whose best similarity to the earlier valid challenges of the last
``heat_window`` rounds (and to its model's frozen cross-match ``heat_refs``) is at least ``heat_threshold_pct``
gets a ``heat_penalty`` line; its defense award never changes. Which comparison applies to a log, and the host
work bound, are ``heat_bound.regime`` and ``heat_bound.compare``.

``HeatScorer`` holds the only state: a bounded cache of walks, a bounded memo of round results and a count of
DP cells. The engine, replay and the arena each own one; a module-level call uses a fresh one.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence

from claude_duel.domain.errors import HeatBoundError
from claude_duel.domain.heat_bound import CELL_BUDGET, MAX_CHALLENGES, Hot, Regime, compare, regime
from claude_duel.domain.heat_tokens import REF_CAP, TOKEN_CAP, Profile, digest, profile, raw
from claude_duel.domain.models import AgentSpec, Attack, Challenge, LedgerRule, MatchState, Validation

DefenseFn = Callable[[Challenge], int]
PairFn = Callable[[str, str], int]  # test seam: a fixed similarity in place of the walk and the DP
_Plan = tuple[tuple[str, str, tuple[str, ...]], ...]  # challenge id, its source, reference sources


class HeatScorer:
    """Heat with explicit, bounded state. ``pair`` replaces the comparison (tests); the memo still applies."""

    def __init__(self, pair: PairFn | None = None, *, walks: int = 64, rounds: int = 64) -> None:
        self.cells = 0  # DP cell updates so far (tests and the work bound)
        self._pair = pair
        self._walks: OrderedDict[bytes, Profile | None] = OrderedDict()
        self._memo: OrderedDict[tuple[object, ...], dict[str, Hot]] = OrderedDict()
        self._walk_max, self._memo_max = walks, rounds

    def walk(self, source: str) -> Profile | None:
        key = digest(source)
        if key in self._walks:
            self._walks.move_to_end(key)
            return self._walks[key]
        got = profile(source)
        self._walks[key] = got
        while len(self._walks) > self._walk_max:
            self._walks.popitem(last=False)
        return got

    def similarity(self, a: str, b: str, rule: LedgerRule, room: int = CELL_BUDGET) -> Hot:
        """One pair under ``rule``'s regime."""
        if self._pair is not None:
            return Hot(self._pair(a, b), True)
        width = rule.heat_token_cap or TOKEN_CAP
        got, cells = compare(self.walk(a), self.walk(b), regime(rule), width, rule.heat_threshold_pct, room)
        self.cells += cells
        return got

    def scan(
        self,
        challenges: Iterable[Challenge],
        validations: Mapping[str, Validation],
        round_no: int,
        rule: LedgerRule,
        refs: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, Hot]:
        """challenge id -> best similarity to the valid challenges of the last ``heat_window`` rounds (the
        author's own and, design R5, the opponent's) plus ``refs[author]`` (sources from the same model's last
        matches, frozen in ``match_created``), for this round's valid challenges at or over the threshold only.
        Invalid challenges never take or give heat."""
        if not rule.heat_window:
            return {}
        plan = _plan(challenges, validations, round_no, rule, refs)
        key = (rule.heat_threshold_pct, regime(rule), rule.heat_token_cap, _digests(plan))
        if key in self._memo:
            self._memo.move_to_end(key)
            return dict(self._memo[key])
        out = self._percentages(plan, rule)
        self._memo[key] = dict(out)
        while len(self._memo) > self._memo_max:
            self._memo.popitem(last=False)
        return out

    def _percentages(self, plan: _Plan, rule: LedgerRule) -> dict[str, Hot]:
        out: dict[str, Hot] = {}
        start = self.cells
        for cid, src, past in plan:
            top = Hot(0, True)
            for ref in past:
                got = self.similarity(src, ref, rule, CELL_BUDGET - (self.cells - start))
                if (got.pct, got.exact) > (top.pct, top.exact):  # on a tie the exact one: the true max is that
                    top = got
            if top.pct >= rule.heat_threshold_pct:
                out[cid] = top
        return out

    def heat(
        self,
        challenges: Iterable[Challenge],
        validations: Mapping[str, Validation],
        round_no: int,
        rule: LedgerRule,
        refs: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, int]:
        """``scan`` percents only, for scoring. A display uses ``scan`` so a bound is not shown as exact."""
        return {cid: h.pct for cid, h in self.scan(challenges, validations, round_no, rule, refs).items()}

    def lines(
        self,
        challenges: Iterable[Challenge],
        validations: Mapping[str, Validation],
        attacks: Mapping[str, Attack],
        round_no: int,
        rule: LedgerRule,
        defense: DefenseFn,
        refs: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, int]:
        """challenge id -> its ``heat_penalty`` line (0 at the threshold), for defended heated challenges only.
        ``attacks`` is keyed by challenge id; ``defense(ch)`` is what classic paid that defense."""
        chs = list(challenges)
        hot = self.heat(chs, validations, round_no, rule, refs)
        out: dict[str, int] = {}
        for ch in chs:
            atk = attacks.get(ch.id)
            if ch.id in hot and not (atk is not None and atk.solved):
                out[ch.id] = penalty(defense(ch), hot[ch.id], rule)
        return out

    def bound_ids(
        self,
        challenges: Iterable[Challenge],
        validations: Mapping[str, Validation],
        round_no: int,
        rule: LedgerRule,
        refs: Mapping[str, Sequence[str]] | None = None,
    ) -> list[str]:
        """Ids whose heat is an upper bound, not an exact similarity (``round_scored.heat_bound``)."""
        hot = self.scan(challenges, validations, round_no, rule, refs)
        return sorted(cid for cid, h in hot.items() if not h.exact)


def _digests(plan: _Plan) -> tuple[object, ...]:
    return tuple((cid, digest(src), tuple(digest(s) for s in past)) for cid, src, past in plan)


def _valid(ch: Challenge, validations: Mapping[str, Validation]) -> bool:
    v = validations.get(ch.id)
    return ch.spec is not None and v is not None and v.valid


def _source(ch: Challenge) -> str:
    assert ch.spec is not None
    return ch.spec.source


def _references(window: list[Challenge], history: Sequence[str], rule: LedgerRule) -> list[str]:
    """References for one challenge. Pinned: the window first (newest rounds if it overflows), then the newest
    history. Legacy: the whole set, or ``HeatBoundError`` past ``REF_CAP``."""
    hist = list(history)
    cap = rule.heat_ref_cap
    if cap is None:
        if len(window) + len(hist) > REF_CAP:
            raise HeatBoundError()
        return [_source(c) for c in window] + hist
    chosen = window
    if len(chosen) > cap:
        chosen = sorted(sorted(chosen, key=lambda c: (c.round, c.id))[-cap:], key=lambda c: c.id)
    sources = [_source(c) for c in chosen]
    room = cap - len(sources)
    return sources + (hist[-room:] if room else [])


def _plan(
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    round_no: int,
    rule: LedgerRule,
    refs: Mapping[str, Sequence[str]] | None,
) -> _Plan:
    every = sorted((c for c in challenges if _valid(c, validations)), key=lambda c: c.id)
    current = [c for c in every if c.round == round_no]
    if len(current) > MAX_CHALLENGES:
        raise HeatBoundError()
    window = [c for c in every if round_no - rule.heat_window <= c.round < round_no]
    history = refs or {}
    return tuple((ch.id, _source(ch), tuple(_references(window, history.get(ch.author, ()), rule))) for ch in current)


def penalty(defense: int, h: int, rule: LedgerRule) -> int:
    """``min(D * heat_max_pct // 100, D * (h - thr) // (100 - thr))``; 0 at the threshold, the cap at 100%."""
    thr = rule.heat_threshold_pct
    if h < thr:
        return 0
    ramp = defense * (h - thr) // (100 - thr) if thr < 100 else defense
    return min(defense * rule.heat_max_pct // 100, ramp)


def heat_label(hot: Hot) -> str:
    """``65%`` exact; ``<=70% (bound)`` when only the upper bound was computed."""
    return f"{hot.pct}%" if hot.exact else f"<={hot.pct}% (bound)"


def heat(
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    round_no: int,
    rule: LedgerRule,
    refs: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, int]:
    """``HeatScorer().heat``: a one-off call with no shared state."""
    return HeatScorer().heat(challenges, validations, round_no, rule, refs)


def history_refs(
    past: Iterable[MatchState],
    agents: Iterable[AgentSpec],
    k: int = 3,
    *,
    source_limit: int | None = None,
) -> dict[str, list[str]]:
    """agent name -> valid challenge sources its model (same provider and model) built in its last ``k`` matches,
    oldest first by ``created_seq``. ``source_limit`` keeps the newest sources so a new match stores a bounded
    list; sources heat would not parse are skipped. Frozen into ``match_created`` so heat replays exactly."""
    ordered = sorted(past, key=lambda m: (m.created_seq is None, m.created_seq or 0, m.match_id))
    out: dict[str, list[str]] = {}
    for spec in agents:
        mine = [(m, a.name) for m in ordered for a in m.agents if (a.provider, a.model) == (spec.provider, spec.model)]
        sources = [
            _source(c)
            for m, name in mine[-k:]
            for c in sorted(m.challenges.values(), key=lambda c: c.id)
            if c.author == name and _valid(c, m.validations) and raw(_source(c)) is not None
        ]
        if source_limit is not None:
            sources = sources[-source_limit:]
        if sources:
            out[spec.name] = sources
    return out


__all__ = ["HeatScorer", "Hot", "Regime", "heat", "heat_label", "history_refs", "penalty"]
