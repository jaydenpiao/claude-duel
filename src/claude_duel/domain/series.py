"""Best-of-N series (#38): a pure fold over the series log, layered above matches (no match state changes).

Each game is a normal match. Its seed is ``sha256(series_seed, game_no)`` and sides alternate (the series'
first agent is ``agents[0]`` in odd games). A game is won on ``final_scores`` (handicap offsets included);
a tie goes to whoever spent fewer attack tokens, else it is drawn. The series ends once someone has
``ceil(N/2)`` wins or all N games are played.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.driver import fold_created
from claude_duel.domain.events import check_version, typed
from claude_duel.domain.models import AgentSpec, Event, MatchState, Rules, SandboxLimits

SERIES_CREATED, GAME_FINISHED = "series_created", "game_finished"
SERIES_SCHEMA_VERSION = 1  # Event.v of series events, versioned apart from match events (#139)


class SeriesRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    games: int = Field(default=3, ge=1, le=9, description="best of N (odd)")
    series_max_usd: float = Field(default=2.0, gt=0, description="checked against played games before each game")
    sudden_death: bool = Field(default=True, description="a tied game is decided by one extra 1-round match")

    @field_validator("games")
    @classmethod
    def _odd(cls, games: int) -> int:
        if games % 2 == 0:
            raise ValueError("games must be odd")
        return games


class Game(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    game_no: int
    match_id: str
    winner: str | None
    scores: dict[str, int]
    usd: float = 0.0
    sudden_death: str = ""  # match id of the 1-round decider played after a tie ("" when none)


class SeriesState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series_id: str
    seed: str
    rule: SeriesRule
    agents: tuple[AgentSpec, AgentSpec]
    rules: Rules | None = None  # pinned in series_created (None in older logs: the config at the time applied)
    limits: SandboxLimits | None = None
    games: list[Game] = Field(default_factory=list)

    def wins(self) -> dict[str, int]:
        out = {a.name: 0 for a in self.agents}
        for g in self.games:
            if g.winner is not None:
                out[g.winner] += 1
        return out

    def done(self) -> bool:
        need = self.rule.games // 2 + 1
        return len(self.games) >= self.rule.games or max(self.wins().values()) >= need

    def winner(self) -> str | None:
        if not self.done():
            return None
        (a, wa), (b, wb) = self.wins().items()
        return a if wa > wb else b if wb > wa else None

    def spent_usd(self) -> float:
        return sum(g.usd for g in self.games)

    def next_game(self) -> int:
        return len(self.games) + 1

    def game_id(self, game_no: int) -> str:
        return f"{self.series_id}-g{game_no}"


def game_seed(series_seed: str, game_no: int) -> str:
    return hashlib.sha256(f"{series_seed}:{game_no}".encode()).hexdigest()[:16]


def game_agents(agents: tuple[AgentSpec, AgentSpec], game_no: int) -> tuple[AgentSpec, AgentSpec]:
    """Sides alternate: the series' first agent leads in odd games."""
    a, b = agents
    return (a, b) if game_no % 2 else (b, a)


def game_winner(state: MatchState) -> str | None:
    final = state.final_scores()
    (a, sa), (b, sb) = sorted(final.items())
    if sa != sb:
        return a if sa > sb else b
    spent = {n: sum(x.tokens_used for x in state.attacks.values() if x.attacker == n) for n in (a, b)}
    if spent[a] != spent[b]:
        return a if spent[a] < spent[b] else b
    return None


def game_of(game_no: int, state: MatchState, decider: MatchState | None = None) -> Game:
    """The game's record; a tie is settled by ``decider`` (the sudden-death match) when one was played."""
    winner = game_winner(state)
    if winner is None and decider is not None:
        winner = game_winner(decider)
    return Game(
        game_no=game_no,
        match_id=state.match_id,
        winner=winner,
        scores=state.final_scores(),
        usd=state.usage.usd + (decider.usage.usd if decider is not None else 0.0),
        sudden_death=decider.match_id if decider is not None else "",
    )


def fold(series_id: str, events: Iterable[Event]) -> SeriesState:
    state, it = fold_created(
        events,
        created_type=SERIES_CREATED,
        schema_version=SERIES_SCHEMA_VERSION,
        what="series",
        state_type=SeriesState,
        id_field="series_id",
        id_value=series_id,
    )
    for e in it:
        check_version(e, SERIES_SCHEMA_VERSION, "series event")
        if e.type != GAME_FINISHED:
            raise EventLogCorruptError(f"unknown series event {e.type!r}")
        game = typed(Game, e.data)
        if game.game_no != state.next_game():
            raise EventLogCorruptError(f"game {game.game_no} recorded out of order")
        state.games.append(game)
    return state
