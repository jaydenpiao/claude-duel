"""`duel series`: play or resume a best-of-N, one normal match per game, logged in ``<id>.series.jsonl``."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from claude_duel.app.budget import CostBudget
from claude_duel.app.container import (
    build_budget,
    build_driver_lock,
    build_event_log,
    build_ledger,
    build_series_log,
    build_store,
    pin_agents,
)
from claude_duel.app.replay import replay
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain import series as sr
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import Event, Rules, SandboxLimits, Stage
from claude_duel.observability import log
from claude_duel.ports import EventObserver, Metrics

_LOG = logging.getLogger(__name__)
# A CAPPED game is terminal (ADR 0014): recorded void (or on partial scores), never resumed or replayed.
_COUNTED = frozenset({Stage.COMPLETE, Stage.CAPPED})


def play_series(
    settings: Settings,
    series_id: str,
    rule: sr.SeriesRule,
    metrics: Metrics,
    observer: EventObserver | None = None,
    budget: CostBudget | None = None,
    caps: tuple[SpendCap, ...] = (),
) -> sr.SeriesState:
    """Play games until the series is decided. Resuming keeps the saved agents, seed and rule."""
    with build_driver_lock(settings, series_id, "series"):  # one driver per id; a second fails fast
        return _play_series(settings, series_id, rule, metrics, observer, budget, caps)


def _play_series(
    settings: Settings,
    series_id: str,
    rule: sr.SeriesRule,
    metrics: Metrics,
    observer: EventObserver | None,
    budget: CostBudget | None,
    caps: tuple[SpendCap, ...],
) -> sr.SeriesState:
    log = build_series_log(settings)
    events = log.read(series_id)
    if not events:
        data: dict[str, object] = {
            "seed": settings.seed or sr.game_seed(series_id, 0),
            "rule": rule.model_dump(mode="json"),
            "agents": [a.model_dump(mode="json") for a in pin_agents(settings)],
            "rules": settings.rules.model_dump(mode="json"),
            "limits": settings.limits.model_dump(mode="json"),
        }
        events = [Event(v=sr.SERIES_SCHEMA_VERSION, seq=0, type=sr.SERIES_CREATED, trace_id=series_id, data=data)]
        log.append(series_id, events[0])
    state = sr.fold(series_id, events)
    settings = pinned(settings, state.rules, state.limits, series_id)
    budget = budget or build_budget(settings)  # one run budget across every game of this invocation
    while not state.done():
        check_caps(settings, (SpendCap("series", f"{series_id}-", state.rule.series_max_usd), *caps))
        n = state.next_game()
        game_settings = settings.model_copy(
            update={"agents": sr.game_agents(state.agents, n), "seed": sr.game_seed(state.seed, n)}
        )
        match = play(game_settings, state.game_id(n), metrics, observer, budget)
        if match.stage not in _COUNTED:
            return state  # failed game: `duel series` again resumes it
        decider = None
        if match.stage is Stage.COMPLETE and state.rule.sudden_death and sr.game_winner(match) is None:
            decider_settings = game_settings.model_copy(
                update={"rules": settings.rules.model_copy(update={"rounds": 1}), "seed": sr.game_seed(state.seed, -n)}
            )
            decider = play(decider_settings, f"{state.game_id(n)}-sd", metrics, observer, budget)
            if decider.stage not in _COUNTED:
                return state
        game = sr.game_of(n, match, decider)
        log.append(
            series_id,
            Event(v=sr.SERIES_SCHEMA_VERSION, seq=n, type=sr.GAME_FINISHED, trace_id=series_id, data=game.model_dump()),
        )
        state.games.append(game)
    return state


def pinned(settings: Settings, rules: Rules | None, limits: SandboxLimits | None, log_id: str) -> Settings:
    """Settings with the rules and limits pinned when the series/tournament was created; warns on config drift."""
    rules, limits = rules or settings.rules, limits or settings.limits
    if (rules, limits) != (settings.rules, settings.limits):
        log(_LOG, logging.WARNING, "config differs from the saved series; resuming with the saved rules", id=log_id)
    return settings.model_copy(update={"rules": rules, "limits": limits})


@dataclass(frozen=True)
class SpendCap:
    """a spend ceiling over every match whose id starts with ``prefix``, read from the shared ledger."""

    label: str
    prefix: str
    max_usd: float


def check_caps(settings: Settings, caps: tuple[SpendCap, ...]) -> None:
    ledger = build_ledger(settings)
    for cap in caps:
        spent = ledger.spent_usd(cap.prefix)
        if spent >= cap.max_usd:
            raise BudgetExceededError(f"{cap.label} cost ${spent:.4f}/${cap.max_usd}")


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
        if state.rules is not None and (match.rules, match.limits) != (state.rules, state.limits):
            bad.append(f"game {g.game_no}: rules differ from the pinned series rules")
        decider = store.load(g.sudden_death) if g.sudden_death else None
        if decider is not None and not replay(decider, events.read(g.sudden_death)).consistent:
            bad.append(f"game {g.game_no}: sudden-death replay")
        if sr.game_of(g.game_no, match, decider) != g:
            bad.append(f"game {g.game_no}: result")
    return SeriesReport(not bad, bad)
