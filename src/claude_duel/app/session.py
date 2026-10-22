"""One `duel run`/`resume`: lock, then load or create, then run. The lock covers the whole session."""

from __future__ import annotations

import hashlib
import logging
import secrets

from claude_duel.app.container import build_engine, build_store, pin_agents
from claude_duel.app.framings import framing_text
from claude_duel.config import Settings
from claude_duel.domain.errors import MatchNotFoundError
from claude_duel.domain.models import MatchState
from claude_duel.observability import log
from claude_duel.ports import EventObserver, Metrics

_LOG = logging.getLogger(__name__)


def play(settings: Settings, match_id: str, metrics: Metrics, observer: EventObserver | None = None) -> MatchState:
    """Run or resume a match. A resumed match keeps its saved agents, models, rules and limits.

    ``observer`` sees the match's existing events first (on resume), then each new one once it is
    durable, applied and saved. It is isolated: its failures never affect the match.
    """
    store = build_store(settings)
    with store.lock(match_id):
        try:
            state = store.load(match_id)
        except MatchNotFoundError:
            state = MatchState(
                match_id=match_id,
                secret=secrets.token_hex(32),
                framing_text=framing_text(settings.rules.framing),
                seed=settings.seed or hashlib.sha256(match_id.encode()).hexdigest()[:16],
                agents=pin_agents(settings),
                rules=settings.rules,
                limits=settings.limits,
            )
            store.save(state)
        else:
            _warn_on_drift(settings, state)
        engine = build_engine(settings, metrics, store, state.agents, observer)
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
