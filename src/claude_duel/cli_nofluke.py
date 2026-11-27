"""CLI for the no-fluke commands: duplicate, ladder, ladder-compare, harness and control-variate (kept apart so cli.py
stays small)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from claude_duel.app import ladder_config, repro
from claude_duel.app.container import build_metrics
from claude_duel.app.control_variate import adjust
from claude_duel.app.duplicate import play_duplicate, verify_duplicate
from claude_duel.app.harness import measure
from claude_duel.app.ladder import compare_ladders, play_ladder, project, read_ladder, verify_ladder
from claude_duel.config import Settings
from claude_duel.domain.duplicate import DuplicateRule
from claude_duel.domain.errors import ConfigError
from claude_duel.observability import InMemoryMetrics
from claude_duel.presentation.duplicate import duplicate_lines, duplicate_view
from claude_duel.presentation.ladder import (
    LadderTicker,
    comparison_lines,
    comparison_view,
    ladder_lines,
    ladder_view,
    projection_lines,
    projection_view,
)


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    dup = sub.add_parser("duplicate", help="play or resume a duplicate match (mirrored boards), then verify it")
    dup.add_argument("--duplicate-id", help="explicit id (re-running an existing id resumes it)")
    dup.add_argument("--boards-min", type=int, default=12)
    dup.add_argument("--boards-max", type=int, default=48)
    dup.add_argument("--match-max-usd", type=float, default=2.0, help="no board starts that could pass this")
    dup.add_argument("--format", choices=("text", "json"), default="text")
    _ladder_parser(sub)
    _harness_parser(sub)
    cv = sub.add_parser("control-variate", help="$0 offline luck adjustment of a duplicate match (reported only)")
    cv.add_argument("--duplicate-id", required=True)
    cv.add_argument("--format", choices=("text", "json"), default="text")


def handlers(settings: Settings, args: argparse.Namespace) -> dict[str, Callable[[], int]]:
    return {
        "duplicate": lambda: duplicate(settings, args),
        "ladder": lambda: ladder(settings, args),
        "ladder-compare": lambda: ladder_compare(settings, args),
        "harness": lambda: harness(settings, args),
        "control-variate": lambda: control_variate(settings, args.duplicate_id, args.format),
    }


_LADDER_FLAGS = (  # flag, plan key, type: every plan key can be overridden on the command line (flags win)
    ("--side-a", "sides.a", "models"),
    ("--side-b", "sides.b", "models"),
    ("--start-tier-a", "start_tier.a", int),
    ("--start-tier-b", "start_tier.b", int),
    ("--boards-min", "match.boards_min", int),
    ("--boards-max", "match.boards_max", int),
    ("--boards", "match.boards", int),
    ("--match-max-usd", "budget.match_max_usd", float),
    ("--ladder-max-usd", "budget.ladder_max_usd", float),
    ("--basis", "budget.basis", str),
    ("--cap-build", "caps.build", int),
    ("--cap-allocate", "caps.allocate", int),
    ("--cap-attack", "caps.attack", int),
    ("--max-attempts", "caps.max_attempts", int),
    ("--effort", "caps.effort", str),
    ("--provider-timeout", "limits.provider_timeout_s", int),
    ("--call-deadline", "limits.call_deadline_s", int),
    ("--match-deadline", "limits.match_deadline_s", int),
    ("--run-date", "run_date", str),
)


def _models(value: str) -> list[str]:
    return [m.strip() for m in value.split(",") if m.strip()]


def _ladder_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    lad = sub.add_parser("ladder", help="play or resume a no-fluke ladder of duplicate matches (--dry-run: $0 plan)")
    lad.add_argument("plan", type=Path, help="ladder TOML (examples/ladder-*.toml)")
    lad.add_argument("--dry-run", action="store_true", help="price the ladder (worst and typical) and exit; no calls")
    lad.add_argument("--ladder-id", help="explicit id (re-running an existing id resumes it)")
    lad.add_argument("--watch", action="store_true", help="print one line per half, build and attack as it happens")
    lad.add_argument("--format", choices=("text", "json"), default="text")
    for flag, key, kind in _LADDER_FLAGS:
        lad.add_argument(flag, dest=key, type=_models if kind == "models" else kind, help=f"overrides {key}")

    cmp = sub.add_parser("ladder-compare", help="two runs of one ladder plan: each matchup agrees or is too close")
    cmp.add_argument("first", help="ladder id of run 1")
    cmp.add_argument("second", help="ladder id of run 2 (same plan; every run gets fresh seeds from its id)")
    cmp.add_argument("--format", choices=("text", "json"), default="text")


def _harness_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    har = sub.add_parser("harness", help="$0 no-fluke harness: scripted bots of known strength play duplicate matches")
    har.add_argument("--pct-a", type=int, default=80, help="first bot's capture chance per try (percent)")
    har.add_argument("--pct-b", type=int, default=50)
    har.add_argument("--matches", type=int, default=50)
    har.add_argument("--boards-min", type=int, default=12)
    har.add_argument("--boards-max", type=int, default=48)
    har.add_argument("--seed", default="harness")
    rep = har.add_argument_group("--repro: two-run agreement under the real stop rule (board model stated, no play)")
    rep.add_argument("--repro", action="store_true", help="simulate run pairs instead of playing scripted bots")
    rep.add_argument("--p-win", type=float, default=0.65, help="stronger side's chance to win a decided board")
    rep.add_argument("--p-tie", type=float, default=0.25, help="share of boards that score 0")
    rep.add_argument("--runs", type=int, default=2000, help="matches (or ladder run pairs with --plan)")
    rep.add_argument("--sprt-p1", type=float, default=0.75)
    rep.add_argument(
        "--stop", choices=("margin", "sprt"), default="margin", help="stop rule (sprt: the older win count)"
    )
    rep.add_argument(
        "--boards-from", type=Path, help="JSON list of measured board margins to draw from (--save-margins)"
    )
    rep.add_argument(
        "--symmetric", action="store_true", help="flip --boards-from signs at random: the equal-strength null"
    )
    har.add_argument("--save-margins", type=Path, help="write every played board margin (first bot's) as JSON")
    rep.add_argument("--plan", type=Path, help="ladder TOML: simulate whole ladders (needs --strengths)")
    rep.add_argument("--strengths", help="model=logit,... Bradley-Terry strengths for --plan")


def harness(settings: Settings, args: argparse.Namespace) -> int:
    try:
        rule = DuplicateRule(
            boards_min=args.boards_min, boards_max=args.boards_max, sprt_p1=args.sprt_p1, stop=args.stop
        )
    except ValueError as exc:
        raise ConfigError(f"bad harness: {exc}") from exc
    if args.repro:
        return _repro(args, rule)
    res = measure(
        settings.store_dir / "harness", args.pct_a, args.pct_b, matches=args.matches, rule=rule, seed=args.seed
    )
    print(
        f"{args.pct_a}% vs {args.pct_b}% over {res.matches} matches: first {res.first} · second {res.second}"
        f" · undecided {res.undecided} · first wins {res.first_rate():.1%}"
        f" · first-seat share {res.first_seat_share:.1%} · first ahead on margin {res.margin_first}"
    )
    if args.save_margins:
        args.save_margins.write_text(json.dumps(list(res.margins)))
    return 0


def _repro(args: argparse.Namespace, rule: DuplicateRule) -> int:
    if args.plan is None:
        if args.boards_from:
            margins = [int(x) for x in json.loads(args.boards_from.read_text())]
            draw, model = repro.resample(margins, symmetric=args.symmetric), f"{len(margins)} measured boards"
            model += " (signs flipped: equal-strength null)" if args.symmetric else ""
        else:
            draw, model = repro.coin(args.p_win, args.p_tie), f"{args.p_win:.0%} board edge, {args.p_tie:.0%} ties"
        m = repro.matchup(rule, draw, args.runs, args.seed)
        print(
            f"{model}, {rule.stop} rule, boards {rule.boards_min}-{rule.boards_max}: first backed {m.first:.1%}"
            f" · second backed {m.wrong:.2%} · two runs agree {m.repeat_agree:.1%} · mean boards {m.mean_boards:.1f}"
            f" ({args.runs} runs)"
        )
        return 0
    try:
        strength = {k.strip(): float(v) for k, v in (kv.split("=") for kv in (args.strengths or "").split(",") if kv)}
    except ValueError as exc:
        raise ConfigError("--strengths is model=logit,model=logit,...") from exc
    overrides = {"match.boards_min": rule.boards_min, "match.boards_max": rule.boards_max}
    cfg = ladder_config.load(args.plan, overrides, allow_premium=True)  # a $0 simulation: no model is called
    missing = [m for m in (*cfg.sides.a, *cfg.sides.b) if m not in strength]
    if missing:
        raise ConfigError(f"--strengths lacks {', '.join(missing)}")
    lad = repro.ladder(cfg, strength, args.p_tie, args.runs, args.seed)
    print(
        f"ladder {args.plan.name}, boards {rule.boards_min}-{rule.boards_max}: final ordering agrees"
        f" {lad.ordering_agree:.1%} · matchups agree {lad.matchup_agree:.1%} · opposite winners {lad.contradict:.2%}"
        f" · mean boards {lad.mean_boards:.1f}"
        f" over {lad.mean_steps:.1f} steps ({args.runs} run pairs)"
    )
    return 0


def ladder_compare(settings: Settings, args: argparse.Namespace) -> int:
    cmp = compare_ladders(read_ladder(settings, args.first), read_ladder(settings, args.second))
    print(json.dumps(comparison_view(cmp)) if args.format == "json" else "\n".join(comparison_lines(cmp)))
    return 0


def ladder(settings: Settings, args: argparse.Namespace) -> int:
    overrides = {key: getattr(args, key) for _, key, _ in _LADDER_FLAGS if getattr(args, key) is not None}
    cfg = ladder_config.load(args.plan, overrides, allow_premium=settings.allow_premium)
    proj = project(cfg)
    if args.dry_run or proj.refusals:
        if args.format == "json":
            print(json.dumps(projection_view(proj, cfg.budget.ladder_max_usd)))
        else:
            print("\n".join(projection_lines(proj, cfg.budget.ladder_max_usd)))
        return 1 if proj.refusals else 0
    ladder_id = args.ladder_id or uuid.uuid4().hex[:12]
    ticker = LadderTicker(sys.stdout) if args.watch and args.format == "text" else None
    metrics = build_metrics(settings, ladder_id, InMemoryMetrics())
    state = play_ladder(settings, ladder_id, cfg, metrics, ticker)
    report = verify_ladder(settings, ladder_id)
    if args.format == "json":
        print(json.dumps(ladder_view(state, report.consistent, report.mismatches)))
    else:
        print("\n".join(ladder_lines(state, report.consistent, report.mismatches)))
    return 0 if report.consistent and state.done() else 1


def duplicate(settings: Settings, args: argparse.Namespace) -> int:
    dup_id = args.duplicate_id or uuid.uuid4().hex[:12]
    try:
        rule = DuplicateRule(boards_min=args.boards_min, boards_max=args.boards_max, match_max_usd=args.match_max_usd)
    except ValueError as exc:
        raise ConfigError(f"bad duplicate: {exc}") from exc
    state = play_duplicate(settings, dup_id, rule, build_metrics(settings, dup_id, InMemoryMetrics()))
    report = verify_duplicate(settings, dup_id)
    if args.format == "json":
        print(json.dumps(duplicate_view(state, report.consistent, report.mismatches)))
    else:
        print("\n".join(duplicate_lines(state, report.consistent, report.mismatches)))
    return 0 if report.consistent and state.done() else 1


def control_variate(settings: Settings, duplicate_id: str, fmt: str) -> int:
    res = adjust(settings, duplicate_id)
    if fmt == "json":
        print(json.dumps(dataclasses.asdict(res)))
        return 0
    lo, hi = res.ci95
    print(f"control variate {duplicate_id} (reported only; the result stays on raw boards)")
    for n, (r, b, a) in enumerate(zip(res.raw, res.baseline, res.adjusted, strict=True), 1):
        print(f"board {n}: raw {r:+d} · baseline {b:+d} · adjusted {a:+d}")
    print(f"adjusted margin {sum(res.adjusted):+d} · p={res.p_value:.4f} · 95% CI [{lo:.2f}, {hi:.2f}]")
    return 0
