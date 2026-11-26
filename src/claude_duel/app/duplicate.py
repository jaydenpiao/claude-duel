"""`duel duplicate`: play or resume a duplicate match (mirrored boards of two normal matches), then verify it."""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.app.budget import CostBudget
from claude_duel.app.container import (
    build_budget,
    build_driver_lock,
    build_duplicate_log,
    build_event_log,
    build_store,
    pin_agents,
)
from claude_duel.app.driver import append, open_or_create, pinned
from claude_duel.app.replay import replay
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain.models import MatchState, Stage
from claude_duel.ports import EventObserver, Metrics


def play_duplicate(
    settings: Settings,
    duplicate_id: str,
    rule: dp.DuplicateRule,
    metrics: Metrics,
    observer: EventObserver | None = None,
    budget: CostBudget | None = None,
) -> dp.DuplicateState:
    """Play boards until the stop rule or the cap ends the match. Resuming keeps the saved agents, seed and rule."""
    with build_driver_lock(settings, duplicate_id, "duplicate"):
        log = build_duplicate_log(settings)
        events = open_or_create(
            log,
            duplicate_id,
            schema_version=dp.DUPLICATE_SCHEMA_VERSION,
            created_type=dp.DUPLICATE_CREATED,
            data={
                "seed": settings.seed or dp.board_seed(duplicate_id, 0),
                "rule": rule.model_dump(mode="json"),
                "agents": [a.model_dump(mode="json") for a in pin_agents(settings)],
                "rules": settings.rules.dump(),
                "limits": settings.limits.model_dump(mode="json"),
            },
        )
        state = dp.fold(duplicate_id, events)
        settings = pinned(settings, state.rules, state.limits, duplicate_id)
        budget = budget or build_budget(settings)
        seq = len(events)
        while not state.done():
            if state.boards and state.spent_usd() + dp.next_board_usd(state) > state.rule.match_max_usd:
                append(
                    log,
                    duplicate_id,
                    schema_version=dp.DUPLICATE_SCHEMA_VERSION,
                    seq=seq,
                    event_type=dp.DUPLICATE_CAPPED,
                    data={},
                )
                state.capped = True
                break
            board = _board(settings, state, metrics, observer, budget)
            if board is None:
                return state  # a half failed or was capped: `duel duplicate` again resumes it
            append(
                log,
                duplicate_id,
                schema_version=dp.DUPLICATE_SCHEMA_VERSION,
                seq=seq,
                event_type=dp.BOARD_SCORED,
                data=board.model_dump(),
            )
            state.boards.append(board)
            seq += 1
        return state


def _board(
    settings: Settings,
    state: dp.DuplicateState,
    metrics: Metrics,
    observer: EventObserver | None,
    budget: CostBudget,
) -> dp.Board | None:
    """Both halves on the board seed; ``None`` when a half did not complete (failed or capped: resumable)."""
    k = state.next_board()
    seed = dp.board_seed(state.seed, k)
    halves = tuple(_half(settings, state, k, h, seed, metrics, observer, budget) for h in (0, 1))
    if any(m.stage is not Stage.COMPLETE for m in halves):
        return None
    return dp.board_of(state, k, seed, (halves[0], halves[1]))


def _half(
    settings: Settings,
    state: dp.DuplicateState,
    k: int,
    half: int,
    seed: str,
    metrics: Metrics,
    observer: EventObserver | None,
    budget: CostBudget,
) -> MatchState:
    half_settings = settings.model_copy(update={"agents": dp.half_agents(state.agents, half), "seed": seed})
    return play(half_settings, state.half_id(k, half), metrics, observer, budget)


@dataclass(frozen=True)
class DuplicateReport:
    consistent: bool
    mismatches: list[str]


def verify_duplicate(settings: Settings, duplicate_id: str) -> DuplicateReport:
    """Every half replays under the pinned rules, and each board recomputes from its two matches."""
    state = dp.fold(duplicate_id, build_duplicate_log(settings).read(duplicate_id))
    store, events = build_store(settings), build_event_log(settings)
    bad: list[str] = []
    for b in state.boards:
        halves = (store.load(b.halves[0]), store.load(b.halves[1]))
        for m in halves:
            if not replay(m, events.read(m.match_id)).consistent:
                bad.append(f"board {b.board_no}: {m.match_id} replay")
            if (m.rules, m.limits) != (state.rules, state.limits):
                bad.append(f"board {b.board_no}: {m.match_id} rules differ from the pinned rules")
        if dp.board_of(state, b.board_no, b.seed, halves) != b:
            bad.append(f"board {b.board_no}: result")
    return DuplicateReport(not bad, bad)
