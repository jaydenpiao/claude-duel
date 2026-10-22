"""`duel series`: play or resume a best-of-N, one normal match per game, logged in ``<id>.series.jsonl``."""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.app.budget import CostBudget
from claude_duel.app.container import build_budget, build_event_log, build_series_log, build_store, pin_agents
from claude_duel.app.replay import replay
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain import series as sr
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import Event, Stage
from claude_duel.ports import EventObserver, Metrics


def play_series(
    settings: Settings,
    series_id: str,
    rule: sr.SeriesRule,
    metrics: Metrics,
    observer: EventObserver | None = None,
    budget: CostBudget | None = None,
) -> sr.SeriesState:
    """Play games until the series is decided. Resuming keeps the saved agents, seed and rule."""
    log = build_series_log(settings)
    events = log.read(series_id)
    if not events:
        data: dict[str, object] = {
            "seed": settings.seed or sr.game_seed(series_id, 0),
            "rule": rule.model_dump(mode="json"),
            "agents": [a.model_dump(mode="json") for a in pin_agents(settings)],
        }
        events = [Event(v=ev.SCHEMA_VERSION, seq=0, type=sr.SERIES_CREATED, trace_id=series_id, data=data)]
        log.append(series_id, events[0])
    state = sr.fold(series_id, events)
    budget = budget or build_budget(settings)  # R3-1: one run budget across every game of this invocation
    while not state.done():
        if state.spent_usd() >= state.rule.series_max_usd:
            raise BudgetExceededError(f"series cost ${state.spent_usd():.4f}/${state.rule.series_max_usd}")
        n = state.next_game()
        game_settings = settings.model_copy(
            update={"agents": sr.game_agents(state.agents, n), "seed": sr.game_seed(state.seed, n)}
        )
        match = play(game_settings, state.game_id(n), metrics, observer, budget)
        if match.stage is not Stage.COMPLETE:
            return state  # failed game: `duel series` again resumes it
        game = sr.game_of(n, match)
        log.append(
            series_id,
            Event(v=ev.SCHEMA_VERSION, seq=n, type=sr.GAME_FINISHED, trace_id=series_id, data=game.model_dump()),
        )
        state.games.append(game)
    return state


@dataclass(frozen=True)
class SeriesReport:
    consistent: bool
    mismatches: list[str]


def verify_series(settings: Settings, series_id: str) -> SeriesReport:
    """Every game's match log replays, and each recorded game winner recomputes from its match."""
    state = sr.fold(series_id, build_series_log(settings).read(series_id))
    store, events = build_store(settings), build_event_log(settings)
    bad: list[str] = []
    for g in state.games:
        match = store.load(g.match_id)
        if not replay(match, events.read(g.match_id)).consistent:
            bad.append(f"game {g.game_no}: replay")
        if sr.game_of(g.game_no, match) != g:
            bad.append(f"game {g.game_no}: result")
    return SeriesReport(not bad, bad)
