"""Tournaments (#39): round robin for <= 6 entrants, Swiss for more. Pure pairing and standings.

Every pairing is a best-of-N series (``domain/series.py``). Standings are 1 point per series win and 0.5
per drawn series. Ties are broken by ``sha256(seed:name)``, so pairings are deterministic for a seed.
Swiss pairs neighbours in the standings and avoids rematches while that is possible; an odd entrant out
gets a bye (1 point), never twice while avoidable.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import AgentSpec, Event
from claude_duel.domain.series import SeriesRule

TOURNAMENT_CREATED, PAIRING_FINISHED = "tournament_created", "pairing_finished"
BYE = "-"
ROUND_ROBIN_MAX = 6

Pair = tuple[str, str]


class TournamentRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entrants: tuple[AgentSpec, ...] = Field(min_length=3, max_length=32)
    format: str = Field(default="auto", pattern="^(auto|round_robin|swiss)$")
    swiss_rounds: int | None = Field(default=None, ge=1, le=20, description="default ceil(log2(entrants))")
    series: SeriesRule = SeriesRule(games=1)
    tournament_max_usd: float = Field(default=5.0, gt=0)

    @field_validator("entrants")
    @classmethod
    def _unique(cls, entrants: tuple[AgentSpec, ...]) -> tuple[AgentSpec, ...]:
        names = [e.name for e in entrants]
        if len(set(names)) != len(names):
            raise ValueError("entrant names must be unique")
        return entrants

    def kind(self) -> str:
        if self.format != "auto":
            return self.format
        return "round_robin" if len(self.entrants) <= ROUND_ROBIN_MAX else "swiss"

    def rounds(self) -> int:
        n = len(self.entrants)
        if self.kind() == "round_robin":
            return n - 1 if n % 2 == 0 else n
        return self.swiss_rounds or math.ceil(math.log2(n))


class Pairing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    round: int
    a: str
    b: str  # BYE for a bye
    winner: str | None
    series_id: str = ""
    usd: float = 0.0


class TournamentState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tournament_id: str
    seed: str
    rule: TournamentRule
    pairings: list[Pairing] = Field(default_factory=list)

    def names(self) -> list[str]:
        return [e.name for e in self.rule.entrants]

    def standings(self) -> dict[str, float]:
        points = dict.fromkeys(self.names(), 0.0)
        for p in self.pairings:
            if p.b == BYE:
                points[p.a] += 1
            elif p.winner is None:
                points[p.a] += 0.5
                points[p.b] += 0.5
            else:
                points[p.winner] += 1
        return points

    def table(self) -> list[tuple[str, float]]:
        pts = self.standings()
        return sorted(pts.items(), key=lambda kv: (-kv[1], tiebreak(self.seed, kv[0])))

    def played(self) -> set[frozenset[str]]:
        return {frozenset((p.a, p.b)) for p in self.pairings}

    def round_pairs(self, round_no: int) -> list[Pair]:
        """The pairs of ``round_no``: fixed up front for round robin; from the standings before it for Swiss."""
        if self.rule.kind() == "round_robin":
            return round_robin(self.names())[round_no - 1]
        before = TournamentState(
            tournament_id=self.tournament_id,
            seed=self.seed,
            rule=self.rule,
            pairings=[p for p in self.pairings if p.round < round_no],
        )
        return swiss_round(before.table(), before.played(), {p.a for p in before.pairings if p.b == BYE})

    def next_pair(self) -> tuple[int, Pair] | None:
        done = {(p.round, p.a, p.b) for p in self.pairings}
        for r in range(1, self.rule.rounds() + 1):
            for pair in self.round_pairs(r):
                if (r, *pair) not in done:
                    return r, pair
        return None

    def spent_usd(self) -> float:
        return sum(p.usd for p in self.pairings)


def tiebreak(seed: str, name: str) -> str:
    return hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()


def round_robin(names: list[str]) -> list[list[Pair]]:
    """Circle method: every pair meets once; with an odd count each entrant sits out (BYE) once."""
    ring = [*names, BYE] if len(names) % 2 else list(names)
    n = len(ring)
    rounds = []
    for _ in range(n - 1):
        pairs = [(ring[i], ring[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) if b == BYE else (b, a) if a == BYE else (a, b) for a, b in pairs])
        ring = [ring[0], ring[-1], *ring[1:-1]]
    return rounds


def swiss_round(table: list[tuple[str, float]], played: set[frozenset[str]], had_bye: set[str]) -> list[Pair]:
    """Pair down the standings, avoiding rematches while possible (backtracking); bye to the lowest eligible."""
    order = [name for name, _ in table]
    bye: list[Pair] = []
    if len(order) % 2:
        out = next((n for n in reversed(order) if n not in had_bye), order[-1])
        order.remove(out)
        bye = [(out, BYE)]
    pairs = _pair(order, played) or _pair(order, set())
    assert pairs is not None
    return pairs + bye


def _pair(order: list[str], played: set[frozenset[str]]) -> list[Pair] | None:
    if not order:
        return []
    first, rest = order[0], order[1:]
    for i, other in enumerate(rest):
        if frozenset((first, other)) in played:
            continue
        tail = _pair(rest[:i] + rest[i + 1 :], played)
        if tail is not None:
            return [(first, other), *tail]
    return None


def fold(tournament_id: str, events: Iterable[Event]) -> TournamentState:
    it = iter(events)
    first = next(it, None)
    if first is None or first.type != TOURNAMENT_CREATED:
        raise EventLogCorruptError("tournament log does not start with tournament_created")
    state = TournamentState.model_validate({"tournament_id": tournament_id, **first.data})
    for e in it:
        if e.type != PAIRING_FINISHED:
            raise EventLogCorruptError(f"unknown tournament event {e.type!r}")
        p = Pairing.model_validate(e.data)
        if state.next_pair() != (p.round, (p.a, p.b)):
            raise EventLogCorruptError(f"pairing {p.a} v {p.b} (round {p.round}) recorded out of order")
        state.pairings.append(p)
    return state
