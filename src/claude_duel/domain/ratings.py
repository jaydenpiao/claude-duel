"""Ratings (#39): Elo for display and Bradley-Terry with bootstrap CIs as the reported number. Pure, offline.

An entrant is ``provider:model`` (fake agents also carry their skill, since they share one model name).
Each completed match is one result, decided on raw scores (``raw_winner``: handicap score offsets never
count); a handicapped agent is rated as a separate entrant (``entrant`` appends the handicap). Elo folds
results in recorded play order: ``created_seq`` (a counter the store writes into ``match_created``, so replay
never depends on file names, mtimes or the clock), then ``match_id`` with digit runs compared as numbers (a
series' ``-g2`` before ``-g10``, a ``-g2-sd`` decider right after ``-g2``); older logs sort first (#137).
Elo is display only; Bradley-Terry is the reported rating.
Bradley-Terry is fitted on the multiset of results,
so it does not depend on file or log order; its bootstrap resamples with a fixed seed.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass

from claude_duel.domain.models import AgentSpec, MatchState, Stage

ELO_START, ELO_K = 1500.0, 32.0


def play_order(created_seq: int | None, match_id: str) -> tuple[int, tuple[tuple[int, str], ...]]:
    """Sort key for Elo: store creation order, then the match id with digit runs compared numerically."""
    return -1 if created_seq is None else created_seq, tuple(
        (int(p), "") if p.isdigit() else (-1, p) for p in re.split(r"(\d+)", match_id) if p
    )


@dataclass(frozen=True, order=True)
class Result:
    match_id: str
    a: str
    b: str
    score_a: float  # 1 win, 0.5 draw, 0 loss
    created_seq: int | None = None  # from match_created (None in older logs)

    @property
    def order(self) -> tuple[int, tuple[tuple[int, str], ...]]:
        return play_order(self.created_seq, self.match_id)


@dataclass(frozen=True)
class Rating:
    entrant: str
    games: int
    elo: float
    bt: float
    lo: float
    hi: float


def entrant(agent: AgentSpec) -> str:
    """Rating identity. A handicapped agent is a separate entrant, never pooled with the plain one."""
    base = (
        f"fake:{agent.model or 'fake'}@skill{agent.skill}"
        if agent.provider == "fake"
        else f"{agent.provider}:{agent.model or 'default'}"
    )
    h = agent.handicap
    if not h.is_set():
        return base
    return f"{base}+hc({h.attempts_delta:+d},x{h.token_budget_mult:g},x{h.bank_mult:g})"


def raw_winner(state: MatchState) -> str | None:
    """Winner on raw ``scores`` (no ``score_offset``), then fewer attack tokens; None on a full tie."""
    (a, sa), (b, sb) = sorted((x.name, state.scores.get(x.name, 0)) for x in state.agents)
    if sa != sb:
        return a if sa > sb else b
    spent = {n: sum(x.tokens_used for x in state.attacks.values() if x.attacker == n) for n in (a, b)}
    if spent[a] != spent[b]:
        return a if spent[a] < spent[b] else b
    return None


def results(states: Iterable[MatchState]) -> list[Result]:
    """One result per completed match between two different entrants, in play order (``play_order``)."""
    out = []
    for s in states:
        if s.stage is not Stage.COMPLETE:
            continue
        x, y = s.agents
        a, b = entrant(x), entrant(y)
        if a == b:
            continue
        win = raw_winner(s)
        out.append(Result(s.match_id, a, b, 0.5 if win is None else 1.0 if win == x.name else 0.0, s.created_seq))
    return sorted(out, key=lambda r: r.order)


def elo(rs: Iterable[Result], k: float = ELO_K) -> dict[str, float]:
    r: dict[str, float] = {}
    for x in sorted(rs, key=lambda r: r.order):
        ra, rb = r.setdefault(x.a, ELO_START), r.setdefault(x.b, ELO_START)
        expected = 1 / (1 + 10 ** ((rb - ra) / 400))
        r[x.a] = ra + k * (x.score_a - expected)
        r[x.b] = rb - k * (x.score_a - expected)
    return r


def _mm_denominator(n: str, p: dict[str, float], games: list[tuple[str, str, float]]) -> float:
    """One MM step's denominator for ``n``: the virtual game vs the anchor (strength 1) plus its real games."""
    return 1 / (p[n] + 1.0) + sum(1 / (p[a] + p[b]) for a, b, _ in games if n in (a, b))


def bradley_terry(rs: Iterable[Result], iters: int = 200) -> dict[str, float]:
    """MM fit (Hunter 2004) with draws as half wins and one virtual draw per entrant against a 1500 anchor,
    so undefeated or winless entrants stay finite. Reported on the Elo scale, anchored at 1500."""
    games = [(x.a, x.b, x.score_a) for x in rs]
    names = sorted({n for a, b, _ in games for n in (a, b)})
    if not names:
        return {}
    wins = dict.fromkeys(names, 0.5)  # the virtual draw
    for a, b, s in games:
        wins[a] += s
        wins[b] += 1 - s
    p = dict.fromkeys(names, 1.0)
    for _ in range(iters):
        p = {n: wins[n] / _mm_denominator(n, p, games) for n in names}
    return {n: ELO_START + 400 * math.log10(v) for n, v in p.items()}


def ratings(rs: list[Result], boot: int = 200, seed: int = 0) -> list[Rating]:
    """Ratings sorted best first; ``lo``/``hi`` are the bootstrap 2.5th/97.5th percentiles of the BT score over the
    resamples that include the entrant (no 1500 fill-in)."""
    rs = sorted(rs)
    point, display = bradley_terry(rs), elo(rs)
    rng = random.Random(seed)  # noqa: S311 - a reproducible bootstrap, not a secret
    samples: dict[str, list[float]] = {n: [] for n in point}
    for _ in range(boot):
        fit = bradley_terry([rng.choice(rs) for _ in rs], iters=60)
        for n, v in fit.items():  # an entrant left out of a resample has no estimate there (#138)
            samples[n].append(v)
    count = {n: sum(n in (x.a, x.b) for x in rs) for n in point}
    out = []
    for n, v in point.items():
        s = sorted(samples[n]) or [v]
        lo, hi = s[int(0.025 * (len(s) - 1))], s[math.ceil(0.975 * (len(s) - 1))]
        out.append(Rating(n, count[n], display[n], v, lo, hi))
    return sorted(out, key=lambda r: (-r.bt, r.entrant))
