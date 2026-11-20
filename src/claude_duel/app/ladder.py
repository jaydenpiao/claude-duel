"""`duel ladder`: the no-fluke ladder of duplicate matches, its $0 dry run, the per-call hard stop, verification and
the two-run comparison."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from claude_duel.app import calibration
from claude_duel.app.budget import CostBudget, SpendCap
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
    calibrated_usd: float = 0.0  # boards_max boards at each model's measured token use (app/calibration.py)


@dataclass(frozen=True)
class Projection:
    rows: list[Row]  # the dearest path through the ladder
    total_usd: float  # worst case of that path
    refusals: list[str]  # non-empty: the ladder must not start
    basis: str = "worst"  # what the refusals were checked against

    def typical_usd(self) -> float:
        """The same path at typical cost: the expected spend, and the refusal basis when ``basis = "typical"``."""
        return sum(r.typical_usd for r in self.rows)

    def calibrated_usd(self) -> float:
        """The same path at each model's measured token use: the refusal basis when ``basis = "calibrated"``."""
        return sum(r.calibrated_usd for r in self.rows)


def match_usd(cfg: LadderConfig, a: str, b: str, *, worst: bool = True, basis: str = "") -> float:
    """One duplicate match: two halves per board, each half a match where both agents play. ``basis`` "calibrated"
    prices each model at its measured token use; otherwise ``worst`` picks the worst or the typical case."""
    rules, rule = cfg.rules(), cfg.duplicate_rule()
    if basis == "calibrated":
        return rule.boards_max * 2 * (calibration.agent_usd(rules, a) + calibration.agent_usd(rules, b))
    tries = 1 + rule.fault_replays if worst else 1
    half = agent_usd(rules, a, worst=worst) + agent_usd(rules, b, worst=worst)
    return rule.boards_max * tries * 2 * half


def basis_usd(cfg: LadderConfig, a: str, b: str) -> float:
    """One match priced on the plan's own ``budget.basis``."""
    return match_usd(cfg, a, b, worst=cfg.budget.basis == "worst", basis=cfg.budget.basis)


def project(cfg: LadderConfig) -> Projection:
    """The dearest ladder path (the most expensive sequence of step-downs), priced at $0 with no model calls.
    Refusals use ``budget.basis``: every reachable match and the path must fit their caps at that basis."""
    a, b = cfg.sides.a, cfg.sides.b

    @cache
    def dearest(i: int, j: int) -> tuple[float, tuple[tuple[int, int], ...]]:
        if i >= len(a) or j >= len(b):
            return 0.0, ()
        here = basis_usd(cfg, a[i], b[j])
        best = max((dearest(*n) for n in {(i + 1, j), (i, j + 1), (i + 1, j + 1)}), key=lambda t: (t[0], len(t[1])))
        return here + best[0], ((i, j), *best[1])

    start = (cfg.start_tier.a, cfg.start_tier.b)
    path_usd, path = dearest(*start)
    rows = [_row(cfg, a[i], b[j]) for i, j in path]
    cap, label = cfg.budget.match_max_usd, f"{cfg.budget.basis} case"
    reachable = [(m, n) for m in a[start[0] :] for n in b[start[1] :]]
    refusals = [
        f"{m} vs {n}: {label} ${usd:.2f} > match_max_usd ${cap:.2f}"
        for m, n in reachable
        if (usd := basis_usd(cfg, m, n)) > cap
    ]
    if path_usd > cfg.budget.ladder_max_usd:
        refusals.append(f"ladder {label} ${path_usd:.2f} > ladder_max_usd ${cfg.budget.ladder_max_usd:.2f}")
    return Projection(rows, sum(r.worst_usd for r in rows), refusals, cfg.budget.basis)


def _row(cfg: LadderConfig, a: str, b: str) -> Row:
    try:
        calibrated = match_usd(cfg, a, b, basis="calibrated")
    except ConfigError:
        if cfg.budget.basis == "calibrated":
            raise
        calibrated = float("nan")  # shown as unknown; only a calibrated plan needs every model measured
    return Row(a, b, match_usd(cfg, a, b), match_usd(cfg, a, b, worst=False), calibrated)


def ladder_settings(settings: Settings, cfg: LadderConfig) -> Settings:
    """Rules from the plan; dollars, not per-half token or call counts, bound a ladder: its run cap is the ladder
    cap and each half may spend up to the match cap (the per-call hard stop keeps the duplicate match under it)."""
    limits = settings.budget.model_copy(
        update={
            "match_max_usd": cfg.budget.match_max_usd,
            "match_max_tokens": LADDER_HALF_MAX_TOKENS,
            "match_max_calls": LADDER_HALF_MAX_CALLS,
            "run_max_usd": cfg.budget.ladder_max_usd,
            "run_max_calls": LADDER_RUN_MAX_CALLS,
        }
    )
    return settings.model_copy(update={"rules": cfg.rules(), "budget": limits})


LADDER_HALF_MAX_TOKENS = 5_000_000
LADDER_HALF_MAX_CALLS = 400
LADDER_RUN_MAX_CALLS = 100_000


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
        settings = ladder_settings(settings, cfg)
        budget = budget or build_budget(settings)
        spent = build_ledger(settings)
        while not state.done():
            n = len(state.steps) + 1
            a, b = state.next_pair()
            if spent.spent_usd(f"{ladder_id}-") + basis_usd(cfg, a, b) > cfg.budget.ladder_max_usd:
                append(
                    log, ladder_id, schema_version=ld.LADDER_SCHEMA_VERSION, seq=n, event_type=ld.LADDER_CAPPED, data={}
                )
                state.capped = True
                break
            step_settings = settings.model_copy(update={"agents": agents(a, b)})
            budget.hard_caps = (
                SpendCap("ladder", f"{ladder_id}-", cfg.budget.ladder_max_usd),
                SpendCap(f"match {state.step_id(n)}", f"{state.step_id(n)}-", cfg.budget.match_max_usd),
            )
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
            if step.result == "capped":  # a hard stop cut the match short of its boards: no verdict, stop here
                append(
                    log,
                    ladder_id,
                    schema_version=ld.LADDER_SCHEMA_VERSION,
                    seq=n + 1,
                    event_type=ld.LADDER_CAPPED,
                    data={},
                )
                state.capped = True
        return state


@dataclass(frozen=True)
class LadderReport:
    consistent: bool
    mismatches: list[str]


def read_ladder(settings: Settings, ladder_id: str) -> ld.LadderState:
    return ld.fold(ladder_id, build_ladder_log(settings).read(ladder_id))


def verify_ladder(settings: Settings, ladder_id: str) -> LadderReport:
    """The plan digest recomputes, and every step's duplicate match verifies and gives the recorded result."""
    state = read_ladder(settings, ladder_id)
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


@dataclass(frozen=True)
class Matchup:
    a: str
    b: str
    results: tuple[str, str]  # each run's result for this pairing ("-" when that run never played it)
    verdict: str  # "agree" or "too close to call"


@dataclass(frozen=True)
class Comparison:
    matchups: list[Matchup]
    ordering: str  # "agree" when both runs took the same path with the same decided results


def compare_ladders(first: ld.LadderState, second: ld.LadderState) -> Comparison:
    """Two runs of one plan (fresh seeds): a pairing agrees only when both runs played it and backed the same side;
    a split, an undecided result, or a pairing only one run reached is too close to call."""
    if first.digest != second.digest:
        raise ConfigError("ladders ran different plans; compare two runs of the same plan")
    by_run = [{(s.a, s.b): s.result for s in run.steps} for run in (first, second)]
    pairs = list(dict.fromkeys([*by_run[0], *by_run[1]]))
    matchups = []
    for pair in pairs:
        r = (by_run[0].get(pair, "-"), by_run[1].get(pair, "-"))
        decided = r[0] == r[1] and r[0] in ("first", "second")  # "capped" never agrees: it has no verdict
        matchups.append(Matchup(pair[0], pair[1], r, "agree" if decided else "too close to call"))
    same_path = [(s.a, s.b) for s in first.steps] == [(s.a, s.b) for s in second.steps]
    ordering = "agree" if same_path and all(m.verdict == "agree" for m in matchups) else "too close to call"
    return Comparison(matchups, ordering)
