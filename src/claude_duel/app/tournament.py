"""`duel tournament`: play or resume a round robin / Swiss, one best-of-N series per pairing."""

from __future__ import annotations

from claude_duel.app.budget import CostBudget
from claude_duel.app.container import (
    build_budget,
    build_driver_lock,
    build_engine,
    build_series_log,
    build_store,
    build_tournament_log,
    pin_agents,
    pin_entrants,
)
from claude_duel.app.driver import SpendCap, append, check_caps, open_or_create, pinned
from claude_duel.app.series import play_series
from claude_duel.app.session import open_match
from claude_duel.config import Settings
from claude_duel.domain import heat
from claude_duel.domain import series as sr
from claude_duel.domain import tournament as tr
from claude_duel.domain.models import AgentSpec, Challenge
from claude_duel.ports import Metrics


def _child_id(*parts: str) -> str:
    """Join id parts and normalise ``_`` to ``-`` (match ids must stay filesystem-safe)."""
    return "-".join(parts).replace("_", "-")


def series_id(settings: Settings, tid: str, round_no: int, names: list[str], pair: tuple[str, str]) -> str:
    """``<tid>-r<n>-p<i>v<j>`` from entrant indexes (names like ``x_y``/``x-y`` or ``a-b``/``b-c``
    collided). A series already started under the old name-based id keeps that id so it resumes."""
    a, b = pair
    legacy = _child_id(tid, f"r{round_no}", a, b)
    if build_series_log(settings).read(legacy):
        return legacy
    return f"{tid}-r{round_no}-p{names.index(a)}v{names.index(b)}"


def play_tournament(
    settings: Settings, tournament_id: str, rule: tr.TournamentRule, metrics: Metrics
) -> tr.TournamentState:
    """Play pairings in order until none are left. Resuming keeps the saved rule and seed."""
    with build_driver_lock(settings, tournament_id, "tournament"):  # one driver per id; a second fails fast
        return _play_tournament(settings, tournament_id, rule, metrics)


def _play_tournament(
    settings: Settings, tournament_id: str, rule: tr.TournamentRule, metrics: Metrics
) -> tr.TournamentState:
    event_log = build_tournament_log(settings)
    settings = settings.model_copy(update={"rules": heat.pin_heat_bound(settings.rules)})
    events = open_or_create(
        event_log,
        tournament_id,
        schema_version=tr.TOURNAMENT_SCHEMA_VERSION,
        created_type=tr.TOURNAMENT_CREATED,
        data={
            "seed": settings.seed or sr.game_seed(tournament_id, 0),
            "rule": rule.model_copy(update={"entrants": pin_entrants(settings, rule.entrants)}).model_dump(mode="json"),
            "rules": settings.rules.model_dump(mode="json"),
            "limits": settings.limits.model_dump(mode="json"),
        },
    )
    state = tr.fold(tournament_id, events)
    settings = pinned(settings, state.rules, state.limits, tournament_id)
    budget = build_budget(settings)
    # in-flight games count; run caps span the whole tournament
    cap = SpendCap("tournament", f"{tournament_id}-", state.rule.tournament_max_usd)
    specs = {e.name: e for e in state.rule.entrants}
    while (nxt := state.next_pair()) is not None:
        check_caps(settings, (cap,))
        round_no, (a, b) = nxt
        if b == tr.BYE:
            pairing = tr.Pairing(round=round_no, a=a, b=b, winner=a)
        else:
            sid = series_id(settings, tournament_id, round_no, state.names(), (a, b))
            pair_settings = settings.model_copy(
                update={"agents": (specs[a], specs[b]), "seed": sr.game_seed(state.seed, len(state.pairings) + 1)}
            )
            series = play_series(
                pair_settings, sid, state.rule.series, metrics, budget=budget, caps=(cap,), normalize_heat=False
            )
            if not series.done():
                return state  # a failed game: rerun to resume
            pairing = tr.Pairing(
                round=round_no, a=a, b=b, winner=series.winner(), series_id=sid, usd=series.spent_usd()
            )
        append(
            event_log,
            tournament_id,
            schema_version=tr.TOURNAMENT_SCHEMA_VERSION,
            seq=len(state.pairings) + 1,
            event_type=tr.PAIRING_FINISHED,
            data=pairing.model_dump(),
        )
        state.pairings.append(pairing)
    if state.rule.pool and state.pool_points is None:
        state.pool_points = tr.pool_points(
            state.rule.decay, _attack_pool(settings, state, metrics, budget, cap), state.names()
        )
        append(
            event_log,
            tournament_id,
            schema_version=tr.TOURNAMENT_SCHEMA_VERSION,
            seq=len(state.pairings) + 1,
            event_type=tr.POOL_SCORED,
            data={"points": state.pool_points},
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
        mid = _child_id(state.tournament_id, "pool", spec.name)
        agents = pin_agents(settings.model_copy(update={"agents": (spec, AgentSpec(name=tr.POOL))}))
        with store.lock(mid):
            match = open_match(
                store,
                settings,
                mid,
                agents=agents,
                seed=tr.tiebreak(state.seed, spec.name)[:16],
                warn_drift=False,
                pin_heat=False,
            )
            engine = build_engine(settings, metrics, store, match.agents, budget=budget)
            mine = [c for c in pool if c.author != spec.name]
            match = engine.attack_pool(match, mine)
        for c in mine:
            if match.attacks[f"{c.id}:{spec.name}"].solved:
                solvers[c.id].append(spec.name)
    return solvers
