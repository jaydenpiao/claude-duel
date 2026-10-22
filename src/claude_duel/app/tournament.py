"""`duel tournament`: play or resume a round robin / Swiss, one best-of-N series per pairing."""

from __future__ import annotations

from claude_duel.app.container import build_budget, build_tournament_log
from claude_duel.app.series import play_series
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain import tournament as tr
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import Event
from claude_duel.domain.series import game_seed
from claude_duel.ports import Metrics


def play_tournament(
    settings: Settings, tournament_id: str, rule: tr.TournamentRule, metrics: Metrics
) -> tr.TournamentState:
    """Play pairings in order until none are left. Resuming keeps the saved rule and seed."""
    log = build_tournament_log(settings)
    events = log.read(tournament_id)
    if not events:
        data: dict[str, object] = {
            "seed": settings.seed or game_seed(tournament_id, 0),
            "rule": rule.model_dump(mode="json"),
        }
        events = [Event(v=ev.SCHEMA_VERSION, seq=0, type=tr.TOURNAMENT_CREATED, trace_id=tournament_id, data=data)]
        log.append(tournament_id, events[0])
    state = tr.fold(tournament_id, events)
    budget = build_budget(settings)  # R3-1: run caps span the whole tournament
    specs = {e.name: e for e in state.rule.entrants}
    while (nxt := state.next_pair()) is not None:
        if state.spent_usd() >= state.rule.tournament_max_usd:
            raise BudgetExceededError(f"tournament cost ${state.spent_usd():.4f}/${state.rule.tournament_max_usd}")
        round_no, (a, b) = nxt
        if b == tr.BYE:
            pairing = tr.Pairing(round=round_no, a=a, b=b, winner=a)
        else:
            sid = f"{tournament_id}-r{round_no}-{a}-{b}".replace("_", "-")
            pair_settings = settings.model_copy(
                update={"agents": (specs[a], specs[b]), "seed": game_seed(state.seed, len(state.pairings) + 1)}
            )
            series = play_series(pair_settings, sid, state.rule.series, metrics, budget=budget)
            if not series.done():
                return state  # a failed game: rerun to resume
            pairing = tr.Pairing(
                round=round_no, a=a, b=b, winner=series.winner(), series_id=sid, usd=series.spent_usd()
            )
        seq = len(state.pairings) + 1
        log.append(
            tournament_id,
            Event(
                v=ev.SCHEMA_VERSION,
                seq=seq,
                type=tr.PAIRING_FINISHED,
                trace_id=tournament_id,
                data=pairing.model_dump(),
            ),
        )
        state.pairings.append(pairing)
    return state
