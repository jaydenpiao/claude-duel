"""`duel ladder`: the no-fluke ladder of duplicate matches, its $0 worst-case dry run, and verification."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from claude_duel.app.budget import CostBudget
from claude_duel.app.container import (
    build_budget,
    build_driver_lock,
    build_duplicate_log,
    build_ladder_log,
    build_ledger,
)
from claude_duel.app.driver import append, open_or_create
from claude_duel.app.duplicate import play_duplicate, verify_duplicate
from claude_duel.app.estimate import agent_usd
from claude_duel.app.ladder_config import LadderConfig, agents
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain import ladder as ld
from claude_duel.domain.errors import BudgetExceededError, ConfigError
from claude_duel.ports import EventObserver, Metrics


@dataclass(frozen=True)
class Row:
    a: str
    b: str
    worst_usd: float  # boards_max boards, every board replayed fault_replays times, every reply at its cap
    typical_usd: float  # boards_max boards, no retries or replays, assumed reply sizes


@dataclass(frozen=True)
class Projection:
    rows: list[Row]  # the dearest path through the ladder
    total_usd: float
    refusals: list[str]  # non-empty: the ladder must not start

    def typical_usd(self) -> float:
        """The same path at typical cost: an expected figure to pick caps from, never the refusal basis."""
        return sum(r.typical_usd for r in self.rows)


def match_usd(cfg: LadderConfig, a: str, b: str, *, worst: bool = True) -> float:
    """One duplicate match: two halves per board, each half a match where both agents play."""
    rules, rule = cfg.rules(), cfg.duplicate_rule()
    tries = 1 + rule.fault_replays if worst else 1
    half = agent_usd(rules, a, worst=worst) + agent_usd(rules, b, worst=worst)
    return rule.boards_max * tries * 2 * half


def project(cfg: LadderConfig) -> Projection:
    """The worst-case ladder path (the dearest sequence of step-downs), priced at $0 with no model calls."""
    a, b = cfg.sides.a, cfg.sides.b

    @cache
    def dearest(i: int, j: int) -> tuple[float, tuple[tuple[int, int], ...]]:
        if i >= len(a) or j >= len(b):
            return 0.0, ()
        here = match_usd(cfg, a[i], b[j])
        best = max((dearest(*n) for n in {(i + 1, j), (i, j + 1), (i + 1, j + 1)}), key=lambda t: (t[0], len(t[1])))
        return here + best[0], ((i, j), *best[1])

    start = (cfg.start_tier.a, cfg.start_tier.b)
    total, path = dearest(*start)
    rows = [Row(a[i], b[j], match_usd(cfg, a[i], b[j]), match_usd(cfg, a[i], b[j], worst=False)) for i, j in path]
    cap = cfg.budget.match_max_usd
    reachable = [(m, n) for m in a[start[0] :] for n in b[start[1] :]]
    refusals = [
        f"{m} vs {n}: worst case ${usd:.2f} > match_max_usd ${cap:.2f}"
        for m, n in reachable
        if (usd := match_usd(cfg, m, n)) > cap
    ]
    if total > cfg.budget.ladder_max_usd:
        refusals.append(f"ladder worst case ${total:.2f} > ladder_max_usd ${cfg.budget.ladder_max_usd:.2f}")
    return Projection(rows, total, refusals)


def play_ladder(
    settings: Settings,
    ladder_id: str,
    cfg: LadderConfig,
    metrics: Metrics,
    observer: EventObserver | None = None,
    budget: CostBudget | None = None,
) -> ld.LadderState:
    """Dry-run first (refuse before any call), then play steps until a side runs out or the ladder cap stops it.
    Resuming keeps the logged plan; a different plan for the same id is refused."""
    refusals = project(cfg).refusals
    if refusals:
        raise BudgetExceededError("ladder refused by the dry run: " + "; ".join(refusals))
    with build_driver_lock(settings, ladder_id, "ladder"):
        log = build_ladder_log(settings)
        events = open_or_create(
            log,
            ladder_id,
            schema_version=ld.LADDER_SCHEMA_VERSION,
            created_type=ld.LADDER_CREATED,
            data={
                "config": cfg.model_dump(mode="json"),
                "digest": cfg.digest(),
                "a": list(cfg.sides.a),
                "b": list(cfg.sides.b),
                "start": [cfg.start_tier.a, cfg.start_tier.b],
            },
        )
        state = ld.fold(ladder_id, events)
        if state.digest != cfg.digest():
            raise ConfigError(f"ladder {ladder_id} was created with another plan (digest {state.digest[:12]})")
        settings = settings.model_copy(update={"rules": cfg.rules()})
        budget = budget or build_budget(settings)
        spent = build_ledger(settings)
        while not state.done():
            n = len(state.steps) + 1
            a, b = state.next_pair()
            if spent.spent_usd(f"{ladder_id}-") + match_usd(cfg, a, b) > cfg.budget.ladder_max_usd:
                append(
                    log, ladder_id, schema_version=ld.LADDER_SCHEMA_VERSION, seq=n, event_type=ld.LADDER_CAPPED, data={}
                )
                state.capped = True
                break
            step_settings = settings.model_copy(update={"agents": agents(a, b)})
            dup = play_duplicate(step_settings, state.step_id(n), cfg.duplicate_rule(), metrics, observer, budget)
            if not dup.done():
                return state  # a half failed: `duel ladder` again resumes it
            step = ld.Step(step_no=n, a=a, b=b, duplicate_id=dup.duplicate_id, result=dup.result(), usd=dup.spent_usd())
            append(
                log,
                ladder_id,
                schema_version=ld.LADDER_SCHEMA_VERSION,
                seq=n,
                event_type=ld.STEP_FINISHED,
                data=step.model_dump(),
            )
            state.steps.append(step)
        return state


@dataclass(frozen=True)
class LadderReport:
    consistent: bool
    mismatches: list[str]


def verify_ladder(settings: Settings, ladder_id: str) -> LadderReport:
    """The plan digest recomputes, and every step's duplicate match verifies and gives the recorded result."""
    state = ld.fold(ladder_id, build_ladder_log(settings).read(ladder_id))
    bad: list[str] = []
    if LadderConfig.model_validate(state.config).digest() != state.digest:
        bad.append("plan digest")
    for s in state.steps:
        report = verify_duplicate(settings, s.duplicate_id)
        if not report.consistent:
            bad.append(f"step {s.step_no}: " + ", ".join(report.mismatches))
        dup = dp.fold(s.duplicate_id, build_duplicate_log(settings).read(s.duplicate_id))
        if dup.result() != s.result:
            bad.append(f"step {s.step_no}: result")
    return LadderReport(not bad, bad)
