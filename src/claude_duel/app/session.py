"""One `duel run`/`resume`: lock, then load or create, then run. The lock covers the whole session."""

from __future__ import annotations

import hashlib
import logging
import secrets

from claude_duel.app import allowance
from claude_duel.app.budget import CostBudget
from claude_duel.app.container import build_engine, build_event_log, build_store, pin_agents
from claude_duel.app.framings import framing_text
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain import heat
from claude_duel.domain.errors import EventLogCorruptError, MatchNotFoundError
from claude_duel.domain.heat_bound import admit
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
    """Load or create a match. New matches pin ``framing_text`` from the current framing rule and store
    ``settings.rules`` as given (heat is pinned in config; a driver child gets the driver's logged rules)."""
    try:
        state = store.load(match_id)
    except MatchNotFoundError:
        rules = settings.rules
        state = MatchState(
            match_id=match_id,
            secret=secrets.token_hex(32),
            framing_text=framing_text(settings.rules.framing),
            seed=seed if seed is not None else (settings.seed or hashlib.sha256(match_id.encode()).hexdigest()[:16]),
            agents=agents if agents is not None else pin_agents(settings),
            rules=rules,
            limits=settings.limits,
        )
        if rules.ledger is not None and rules.ledger.heat_window:
            state.heat_refs = heat.history_refs(
                _history(settings, match_id),
                state.agents,
                source_limit=rules.ledger.heat_ref_cap,
                token_limit=admit(rules) if rules.ledger.heat_compare == "full" else None,
            )
        store.save(state)
        if (why := allowance.wallets_exceed(state.rules, len(state.agents), settings.budget.match_max_usd)) is not None:
            log(_LOG, logging.WARNING, why, match_id=match_id)
    else:
        if warn_drift:
            _warn_on_drift(settings, state)
    return state


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


def _history(settings: Settings, match_id: str) -> list[MatchState]:
    """Other matches in this store, folded from their logs (no secrets); unreadable logs are skipped."""
    log_ = build_event_log(settings)
    out = []
    for other in log_.ids():
        if other == match_id:
            continue
        try:
            out.append(ev.fold(other, log_.read(other)))
        except (EventLogCorruptError, ValueError, OSError, KeyError):
            continue
    return out


def _warn_on_drift(settings: Settings, state: MatchState) -> None:
    current = (pin_agents(settings), settings.rules, settings.limits)
    if current != (state.agents, state.rules, state.limits):
        log(
            _LOG,
            logging.WARNING,
            "config differs from the saved match; resuming with the saved config",
            match_id=state.match_id,
        )
