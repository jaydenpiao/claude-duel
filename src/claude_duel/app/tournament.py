"""`duel tournament`: play or resume a round robin / Swiss, one best-of-N series per pairing."""

from __future__ import annotations

import secrets

from claude_duel.app.budget import CostBudget
from claude_duel.app.container import (
    build_budget,
    build_engine,
    build_series_log,
    build_store,
    build_tournament_log,
    pin_agents,
    pin_entrants,
)
from claude_duel.app.series import SpendCap, check_caps, pinned, play_series
from claude_duel.config import Settings
from claude_duel.domain import series as sr
from claude_duel.domain import tournament as tr
from claude_duel.domain.errors import MatchNotFoundError
from claude_duel.domain.models import AgentSpec, Challenge, Event, MatchState
from claude_duel.domain.series import game_seed
from claude_duel.ports import Metrics


def series_id(settings: Settings, tid: str, round_no: int, names: list[str], pair: tuple[str, str]) -> str:
    """``<tid>-r<n>-p<i>v<j>`` from entrant indexes (names like ``x_y``/``x-y`` or ``a-b``/``b-c``
    collided). A series already started under the old name-based id keeps that id so it resumes."""
    a, b = pair
    legacy = f"{tid}-r{round_no}-{a}-{b}".replace("_", "-")
    if build_series_log(settings).read(legacy):
        return legacy
    return f"{tid}-r{round_no}-p{names.index(a)}v{names.index(b)}"


def play_tournament(
    settings: Settings, tournament_id: str, rule: tr.TournamentRule, metrics: Metrics
) -> tr.TournamentState:
    """Play pairings in order until none are left. Resuming keeps the saved rule and seed."""
    log = build_tournament_log(settings)
    events = log.read(tournament_id)
    if not events:
        data: dict[str, object] = {
            "seed": settings.seed or game_seed(tournament_id, 0),
            "rule": rule.model_copy(update={"entrants": pin_entrants(settings, rule.entrants)}).model_dump(mode="json"),
            "rules": settings.rules.model_dump(mode="json"),
            "limits": settings.limits.model_dump(mode="json"),
        }
        events = [
            Event(v=tr.TOURNAMENT_SCHEMA_VERSION, seq=0, type=tr.TOURNAMENT_CREATED, trace_id=tournament_id, data=data)
        ]
        log.append(tournament_id, events[0])
    state = tr.fold(tournament_id, events)
    settings = pinned(settings, state.rules, state.limits, tournament_id)
    budget = build_budget(settings)
    cap = SpendCap(
        "tournament", f"{tournament_id}-", state.rule.tournament_max_usd
    )  # in-flight games count  # run caps span the whole tournament
    specs = {e.name: e for e in state.rule.entrants}
    while (nxt := state.next_pair()) is not None:
        check_caps(settings, (cap,))
        round_no, (a, b) = nxt
        if b == tr.BYE:
            pairing = tr.Pairing(round=round_no, a=a, b=b, winner=a)
        else:
            sid = series_id(settings, tournament_id, round_no, state.names(), (a, b))
            pair_settings = settings.model_copy(
                update={"agents": (specs[a], specs[b]), "seed": game_seed(state.seed, len(state.pairings) + 1)}
            )
            series = play_series(pair_settings, sid, state.rule.series, metrics, budget=budget, caps=(cap,))
            if not series.done():
                return state  # a failed game: rerun to resume
            pairing = tr.Pairing(
                round=round_no, a=a, b=b, winner=series.winner(), series_id=sid, usd=series.spent_usd()
            )
        seq = len(state.pairings) + 1
        log.append(
            tournament_id,
            Event(
                v=tr.TOURNAMENT_SCHEMA_VERSION,
                seq=seq,
                type=tr.PAIRING_FINISHED,
                trace_id=tournament_id,
                data=pairing.model_dump(),
            ),
        )
        state.pairings.append(pairing)
    if state.rule.pool and state.pool_points is None:
        state.pool_points = tr.pool_points(
            state.rule.decay, _attack_pool(settings, state, metrics, budget, cap), state.names()
        )
        data = {"points": state.pool_points}
        seq = len(state.pairings) + 1
        log.append(
            tournament_id,
            Event(v=tr.TOURNAMENT_SCHEMA_VERSION, seq=seq, type=tr.POOL_SCORED, trace_id=tournament_id, data=data),
        )
    return state


def pool_challenges(settings: Settings, state: tr.TournamentState) -> list[Challenge]:
    """Every valid challenge built in the tournament's matches, in a stable order."""
    store, series_log = build_store(settings), build_series_log(settings)
    out: list[Challenge] = []
    for p in state.pairings:
        if not p.series_id:
            continue
        for game in sr.fold(p.series_id, series_log.read(p.series_id)).games:
            match = store.load(game.match_id)
            out += [c for c in match.challenges.values() if c.spec is not None and match.validations[c.id].valid]
    return sorted(out, key=lambda c: c.id)


def _attack_pool(
    settings: Settings, state: tr.TournamentState, metrics: Metrics, budget: CostBudget, cap: SpendCap
) -> dict[str, list[str]]:
    """Each entrant attacks every pool challenge it did not write, in its own resumable pool match."""
    pool = pool_challenges(settings, state)
    solvers: dict[str, list[str]] = {c.id: [] for c in pool}
    store = build_store(settings)
    for spec in state.rule.entrants:
        check_caps(settings, (cap,))
        mid = f"{state.tournament_id}-pool-{spec.name}".replace("_", "-")
        agents = pin_agents(settings.model_copy(update={"agents": (spec, AgentSpec(name=tr.POOL))}))
        with store.lock(mid):
            try:
                match = store.load(mid)
            except MatchNotFoundError:
                match = MatchState(
                    match_id=mid,
                    secret=secrets.token_hex(32),
                    seed=tr.tiebreak(state.seed, spec.name)[:16],
                    agents=agents,
                    rules=settings.rules,
                    limits=settings.limits,
                )
                store.save(match)
            engine = build_engine(settings, metrics, store, match.agents, budget=budget)
            mine = [c for c in pool if c.author != spec.name]
            match = engine.attack_pool(match, mine)
        for c in mine:
            if match.attacks[f"{c.id}:{spec.name}"].solved:
                solvers[c.id].append(spec.name)
    return solvers
