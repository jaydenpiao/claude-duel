"""One `duel run`/`resume`: lock, then load or create, then run. The lock covers the whole session."""

from __future__ import annotations

import hashlib
import logging
import secrets

from claude_duel.app.budget import CostBudget
from claude_duel.app.container import build_engine, build_event_log, build_store, pin_agents
from claude_duel.app.framings import framing_text
from claude_duel.config import Settings
from claude_duel.domain import variety
from claude_duel.domain.errors import EventLogCorruptError, MatchNotFoundError
from claude_duel.domain.models import AgentSpec, MatchState
from claude_duel.observability import log
from claude_duel.ports import EventObserver, MatchStore, Metrics

_LOG = logging.getLogger(__name__)


def open_match(
    store: MatchStore,
    settings: Settings,
    match_id: str,
    *,
    agents: tuple[AgentSpec, ...] | None = None,
    seed: str | None = None,
    warn_drift: bool = True,
) -> MatchState:
    """Load or create a match. New matches pin ``framing_text`` from the current framing rule."""
    try:
        state = store.load(match_id)
    except MatchNotFoundError:
        state = MatchState(
            match_id=match_id,
            secret=secrets.token_hex(32),
            framing_text=framing_text(settings.rules.framing),
            seed=seed if seed is not None else (settings.seed or hashlib.sha256(match_id.encode()).hexdigest()[:16]),
            agents=agents if agents is not None else pin_agents(settings),
            rules=settings.rules,
            limits=settings.limits,
        )
        if settings.rules.variety:  # R2: frozen into match_created, so replay never rereads the store
            state.family_bans = variety.history_bans(_history(store, settings, match_id), state.agents)
        store.save(state)
    else:
        if warn_drift:
            _warn_on_drift(settings, state)
    return state


def _history(store: MatchStore, settings: Settings, match_id: str) -> list[MatchState]:
    """Saved matches in this store (secrets never opened); logs that aren't matches are skipped."""
    out = []
    for other in build_event_log(settings).ids():
        if other == match_id:
            continue
        try:
            out.append(store.snapshot(other))
        except (MatchNotFoundError, EventLogCorruptError, ValueError, OSError):
            continue
    return out


def play(
    settings: Settings,
    match_id: str,
    metrics: Metrics,
    observer: EventObserver | None = None,
    budget: CostBudget | None = None,
) -> MatchState:
    """Run or resume a match. A resumed match keeps its saved agents, models, rules and limits.

    ``observer`` sees the match's existing events first (on resume), then each new one once it is
    durable, applied and saved. It is isolated: its failures never affect the match.
    """
    store = build_store(settings)
    with store.lock(match_id):
        state = open_match(store, settings, match_id)
        engine = build_engine(settings, metrics, store, state.agents, observer, budget)
        engine.replay_to_observer(state)
        return engine.run(state)


def _warn_on_drift(settings: Settings, state: MatchState) -> None:
    current = (pin_agents(settings), settings.rules, settings.limits)
    if current != (state.agents, state.rules, state.limits):
        log(
            _LOG,
            logging.WARNING,
            "config differs from the saved match; resuming with the saved config",
            match_id=state.match_id,
        )
