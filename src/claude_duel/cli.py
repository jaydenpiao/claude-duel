"""`duel` command line: run, resume, replay, card, estimate, series, ratings, tournament, bench, sync, export."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from claude_duel.app.backup import backup
from claude_duel.app.bench import run_bench, solve_rate
from claude_duel.app.budget import check_model_allowed
from claude_duel.app.container import (
    build_event_log,
    build_ledger,
    build_metrics,
    build_public_reader,
    build_results,
    build_store,
    git_sha,
    migrate_results,
)
from claude_duel.app.estimate import estimate, render
from claude_duel.app.experiment import footer as experiment_footer
from claude_duel.app.experiment import run_stakes
from claude_duel.app.export import write as write_export
from claude_duel.app.replay import replay
from claude_duel.app.series import SeriesReport, play_series, verify_series
from claude_duel.app.session import play
from claude_duel.app.sync import sync
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings, load_settings
from claude_duel.domain.errors import ConfigError, DuelError
from claude_duel.domain.events import fold
from claude_duel.domain.models import MatchState, Stage
from claude_duel.domain.modes import MODES, preset
from claude_duel.domain.ratings import ratings, results
from claude_duel.domain.scoring import match_stats
from claude_duel.domain.series import SeriesRule, SeriesState
from claude_duel.observability import InMemoryMetrics, configure_logging, log
from claude_duel.presentation.arena import ArenaView
from claude_duel.presentation.card import card_data, card_svg
from claude_duel.presentation.commentary import Commentary

_LOG = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="duel", description="Agent-vs-agent staged CTF arena")
    p.add_argument("--config", type=Path, help="TOML config file")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="start a new match")
    run.add_argument("--match-id", help="explicit id (re-running an existing id resumes it)")
    for name in ("resume", "replay"):
        sp = sub.add_parser(name, help=f"{name} a match")
        sp.add_argument("match_id")
    for name in ("run", "resume"):
        sub.choices[name].add_argument(
            "--format", choices=("text", "json"), default="text", help="text: live commentary; json: one summary"
        )
        sub.choices[name].add_argument(
            "--view",
            choices=("auto", "arena", "commentary"),
            default="auto",
            help="text output style; auto: arena on a terminal, commentary otherwise",
        )
        sub.choices[name].add_argument(
            "--pace", type=float, default=0.0, help="arena view: seconds to pause after each event (demos)"
        )
    card = sub.add_parser("card", help="write a shareable SVG summary of a match")
    card.add_argument("match_id")
    card.add_argument("--out", type=Path, help="output path (default: <match-id>-card.svg)")
    est = sub.add_parser("estimate", help="offline worst-case cost per match for each preset (no API calls)")
    est.add_argument("--mode", help="one preset (default: all)")
    est.add_argument("--model", help="price at this allowlisted model (default: configured openai_model)")
    series = sub.add_parser("series", help="play or resume a best-of-N series of matches, then verify it")
    series.add_argument("--series-id", help="explicit id (re-running an existing id resumes it)")
    series.add_argument("--games", type=int, default=3, help="best of N (odd, default 3)")
    series.add_argument("--format", choices=("text", "json"), default="text")
    rat = sub.add_parser("ratings", help="offline Elo + Bradley-Terry (95%% bootstrap CI) over all completed matches")
    rat.add_argument("--boot", type=int, default=200, help="bootstrap resamples (0: no CI)")
    rat.add_argument("--format", choices=("text", "json"), default="text")
    bench = sub.add_parser("bench", help="solve rate on a local stdin-only task manifest (budget-capped)")
    bench.add_argument("manifest", type=Path, help="JSON-lines tasks (docs/design/benchmark.md)")
    bench.add_argument("--tries", type=int, default=3, help="attempts per task (1-10)")
    bench.add_argument("--format", choices=("text", "json"), default="text")
    tour = sub.add_parser("tournament", help="play or resume the config's [tournament] (round robin or Swiss)")
    tour.add_argument("--tournament-id", help="explicit id (re-running an existing id resumes it)")
    tour.add_argument("--format", choices=("text", "json"), default="text")
    syn = sub.add_parser("sync", help="verify, replay and load every match log into the results DB (DATABASE_URL)")
    syn.add_argument(
        "--rebuild", action="store_true", help="empty and reload the derived tables in one transaction (schema owner)"
    )
    syn.add_argument("--boot", type=int, default=200, help="bootstrap resamples for the ratings snapshot")
    syn.add_argument("--format", choices=("text", "json"), default="text")
    sub.add_parser("backup", help="copy event logs and state (never secrets) to the GCS replay bucket").add_argument(
        "--bucket", help="gs:// destination (default DUEL_REPLAY_BUCKET)"
    )
    db_p = sub.add_parser("db", help="results database administration")
    db_sub = db_p.add_subparsers(dest="db_cmd", required=True)
    db_sub.add_parser("migrate", help="apply pending results DB migrations (MIGRATE_DATABASE_URL, else DATABASE_URL)")
    out_p = sub.add_parser("export", help="write the static public JSON from the read-only views (EXPORT_DATABASE_URL)")
    out_p.add_argument("--out", type=Path, default=Path("llm-duels-export.json"), help="output path")
    exp = sub.add_parser("experiment", help="run a pre-registered experiment (paired framing micro-matches)")
    exp_sub = exp.add_subparsers(dest="experiment", required=True)
    stakes = exp_sub.add_parser("stakes", help="framing A/B/C, one shared run cap (docs/runs/stakes-prereg.md)")
    stakes.add_argument("--prereg", type=Path, required=True, help="pre-registration written before any spend")
    stakes.add_argument("--seeds", type=int, default=1, help="seeds; each runs once per framing")
    stakes.add_argument("--run-max-usd", type=float, help="raise run_max_usd for this run only (max $2.00)")
    stakes.add_argument("--override-reason", default="", help="required with --run-max-usd; logged and ledgered")
    stakes.add_argument("--format", choices=("text", "json"), default="text")
    replay_p = sub.choices["replay"]
    replay_p.add_argument("--transcript", action="store_true")
    return p


def _summary(state: MatchState) -> str:
    u = state.usage
    return json.dumps(
        {
            "match_id": state.match_id,
            "stage": state.stage.value,
            "round": state.round,
            "scores": dict(sorted(state.scores.items())),
            "calls": u.calls,
            "tokens": u.tokens,
            "est_usd": round(u.usd, 6),
            **match_stats(state),
            "findings": dict(sorted(Counter(f.category for f in state.findings).items())),
        }
    )


def _observer(settings: Settings, mid: str, args: argparse.Namespace) -> Commentary | ArenaView | None:
    if args.format != "text":
        return None
    limit, lifetime = settings.budget.lifetime_max_usd, build_ledger(settings).total_usd
    if args.view == "arena" or (args.view == "auto" and sys.stdout.isatty()):
        return ArenaView(Console(), mid, limit, lifetime, pace=max(args.pace, 0.0))
    return Commentary(sys.stdout, mid, limit, lifetime)


def _run(settings: Settings, match_id: str | None, args: argparse.Namespace) -> int:
    metrics = InMemoryMetrics()
    mid = match_id or uuid.uuid4().hex[:12]
    observer = _observer(settings, mid, args)
    try:
        state = play(settings, mid, build_metrics(settings, mid, metrics), observer)
    finally:
        if isinstance(observer, ArenaView):
            observer.close()
        log(_LOG, logging.INFO, "metrics", match_id=mid, **metrics.summary())
    done = 0
    if state.capped is not None:  # ADR 0014: terminal and void; resuming it changes nothing
        hit = state.capped
        print(f"match capped: {hit.limit} {hit.spent:g}/{hit.cap:g} by {hit.breaching_agent} (void)", file=sys.stderr)
        done = 3
    if observer is None:
        print(_summary(state))
        return done
    report = replay(build_store(settings).load(mid), build_event_log(settings).read(mid))
    print(f"replay verified {'✓' if report.consistent else '✗ ' + ', '.join(report.mismatches)}")
    return done if report.consistent else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code = _dispatch(args)
        sys.stdout.flush()
    except DuelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # `duel run | head`: reader left; the match itself is unaffected and saved
        _silence_stdout()
        return 0
    return code


def _dispatch(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    configure_logging(settings.log_level)
    handlers: dict[str, Callable[[], int]] = {
        "run": lambda: _run(settings, args.match_id, args),
        "resume": lambda: _run(settings, args.match_id, args) if _exists(settings, args.match_id) else 2,
        "estimate": lambda: _estimate(settings, args.mode, args.model),
        "series": lambda: _series(settings, args.series_id or uuid.uuid4().hex[:12], args.games, args.format),
        "ratings": lambda: _ratings(settings, max(args.boot, 0), args.format),
        "tournament": lambda: _tournament(settings, args.tournament_id or uuid.uuid4().hex[:12], args.format),
        "card": lambda: _card(settings, args.match_id, args.out),
        "sync": lambda: _sync(settings, args.rebuild, max(args.boot, 0), args.format),
        "export": lambda: _export(settings, args.out),
        "backup": lambda: _backup(settings, args.bucket),
        "db": lambda: _migrate(settings),
        "bench": lambda: _bench(settings, args.manifest, args.tries, args.format),
        "experiment": lambda: _experiment(settings, args),
    }
    if args.cmd in handlers:
        return handlers[args.cmd]()
    report = replay(build_store(settings).load(args.match_id), build_event_log(settings).read(args.match_id))
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.transcript:
        print("\n".join(report.transcript))
    print(
        f"replay consistent={report.consistent} recomputed={report.recomputed} recorded={report.recorded}"
        + (f" mismatches={report.mismatches}" if report.mismatches else "")
    )
    return 0 if report.consistent else 1


def _backup(settings: Settings, bucket: str | None) -> int:
    files = backup(settings.store_dir, bucket or settings.replay_bucket)
    print(f"backed up {len(files)} replay files")
    return 0


def _experiment(settings: Settings, args: argparse.Namespace) -> int:
    if args.override_reason and args.run_max_usd is None:
        raise ConfigError("--override-reason needs --run-max-usd")
    result = run_stakes(settings, args.prereg, args.seeds, InMemoryMetrics(), args.run_max_usd, args.override_reason)
    if args.format == "json":
        print(json.dumps(result))
        return 0
    for m in result["matches"]:
        print(f"{m['match_id']}  {m['seed']}  {m['framing']:<11} {m['stage']}")
    if result["stopped"]:
        print(f"stopped: {result['stopped']}")
    b = result["budget"]
    print(f"{result['run_id']}: {len(result['matches'])} matches, ${b['run_usd']:.4f} of ${b['run_max_usd']:.2f}")
    if line := experiment_footer(result):
        print(line)
    return 0


def _bench(settings: Settings, manifest: Path, tries: int, fmt: str) -> int:
    if not 1 <= tries <= 10:
        raise ConfigError("--tries must be 1-10")
    bench_id = f"bench-{uuid.uuid4().hex[:12]}"
    results, usage = run_bench(settings, manifest, tries, bench_id)
    rates = solve_rate(results)
    spend = {"bench_id": bench_id, "calls": usage.calls, "tokens": usage.tokens, "est_usd": round(usage.usd, 6)}
    if fmt == "json":
        print(json.dumps({**spend, "solve_rate": rates, "results": [r.model_dump() for r in results]}))
        return 0
    for r in results:
        print(f"{r.agent:<10} {r.task_id:<24} {'solved' if r.solved else 'failed'} in {r.attempts} ({r.tokens} tok)")
    print("solve rate: " + ", ".join(f"{a} {v:.0%}" for a, v in rates.items()))
    print(f"{bench_id}: {usage.calls} calls, {usage.tokens} tokens, ${usage.usd:.4f}")
    return 0


def _estimate(settings: Settings, mode: str | None, model: str | None) -> int:
    chosen = model or settings.default_model("openai")
    check_model_allowed(chosen)
    try:
        names = [mode] if mode else sorted(MODES)
        rows = [estimate(preset(n), chosen, settings.budget, settings.agents) for n in names]
    except KeyError as exc:
        raise ConfigError(str(exc.args[0])) from exc
    print(render(rows))
    return 0


def _series(settings: Settings, series_id: str, games: int, fmt: str) -> int:
    try:
        rule = SeriesRule(games=games)
    except ValueError as exc:
        raise ConfigError(f"bad series: {exc}") from exc
    state = play_series(settings, series_id, rule, build_metrics(settings, series_id, InMemoryMetrics()))
    report = verify_series(settings, series_id)
    if fmt == "json":
        print(json.dumps(_series_json(series_id, state, report.consistent)))
    else:
        print("\n".join(_series_text(series_id, state, report)))
    return 0 if report.consistent and state.done() else 1


def _series_json(series_id: str, state: SeriesState, verified: bool) -> dict[str, object]:
    return {
        "series_id": series_id,
        "best_of": state.rule.games,
        "wins": state.wins(),
        "winner": state.winner(),
        "void": state.void(),
        "games": [g.model_dump() for g in state.games],
        "verified": verified,
    }


def _series_text(series_id: str, state: SeriesState, report: SeriesReport) -> list[str]:
    lines = [
        f"game {g.game_no} ({g.match_id}): {'void' if g.void else g.winner or 'draw'} · {g.scores}" for g in state.games
    ]
    winner = state.winner()
    result = "undecided" if not state.done() else "void" if state.void() else f"winner: {winner}" if winner else "drawn"
    lines.append(f"series {series_id} best of {state.rule.games}: {result} · wins {state.wins()}")
    lines.append(f"series verified {'✓' if report.consistent else '✗ ' + ', '.join(report.mismatches)}")
    return lines


def _ratings(settings: Settings, boot: int, fmt: str) -> int:
    logs = build_event_log(settings)
    states, skipped = [], []
    for mid in logs.ids():
        try:
            states.append(fold(mid, logs.read(mid)))
        except DuelError as exc:  # one bad or newer log never aborts the table
            skipped.append(mid)
            log(_LOG, logging.WARNING, "ratings: skipped unreadable match log", match_id=mid, error=str(exc))
    if skipped:
        print(f"ratings: {len(skipped)} log(s) skipped: {', '.join(skipped)}", file=sys.stderr)
    capped = sum(s.stage is Stage.CAPPED for s in states)
    if capped:  # ADR 0014: a capped match is void and never rated
        print(f"ratings: {capped} capped match(es) excluded", file=sys.stderr)
    table = ratings(results(states), boot=boot)
    if fmt == "json":
        print(json.dumps([dataclasses.asdict(r) for r in table]))
        return 0
    print(f"{'entrant':<32}{'games':>6}{'bt':>7}{'95% ci':>14}{'elo':>7}")
    for r in table:
        print(f"{r.entrant:<32}{r.games:>6}{r.bt:>7.0f}{f'{r.lo:.0f}-{r.hi:.0f}':>14}{r.elo:>7.0f}")
    return 0


def _tournament(settings: Settings, tournament_id: str, fmt: str) -> int:
    if settings.tournament is None:
        raise ConfigError("no [tournament] table in the config (needs at least 3 entrants)")
    state = play_tournament(
        settings, tournament_id, settings.tournament, build_metrics(settings, tournament_id, InMemoryMetrics())
    )
    finished = state.next_pair() is None
    if fmt == "json":
        out = {
            "tournament_id": tournament_id,
            "format": state.rule.kind(),
            "finished": finished,
            "standings": state.table(),
            "pool_points": state.pool_points,
            "pairings": [p.model_dump() for p in state.pairings],
        }
        print(json.dumps(out))
    else:
        for p in state.pairings:
            print(f"round {p.round}: {p.a} v {p.b} -> {p.winner or 'draw'}")
        print(f"tournament {tournament_id} ({state.rule.kind()}){'' if finished else ' unfinished'}:")
        for rank, (name, points) in enumerate(state.table(), 1):
            pool = "" if state.pool_points is None else f"  pool {state.pool_points[name]}"
            print(f"{rank:>3}. {name:<24}{points:g}{pool}")
    return 0 if finished else 1


def _card(settings: Settings, match_id: str, out: Path | None) -> int:
    state, events = build_store(settings).load(match_id), build_event_log(settings).read(match_id)
    report = replay(state, events)
    path = out or Path(f"{match_id}-card.svg")
    path.write_text(card_svg(card_data(state, report, events)), encoding="utf-8")
    print(f"card written: {path}")
    return 0 if report.consistent else 1


def _migrate(settings: Settings) -> int:
    applied = migrate_results(settings)
    print(f"migrations applied: {', '.join(applied)}" if applied else "migrations: up to date")
    return 0


def _sync(settings: Settings, rebuild: bool, boot: int, fmt: str) -> int:
    db = build_results(settings, owner=rebuild)
    log, snaps = build_event_log(settings), build_store(settings)
    report = sync(log, snaps, db, boot=boot, rebuild=rebuild, git_sha=git_sha())
    if fmt == "json":
        print(json.dumps(dataclasses.asdict(report)))
    else:
        print(f"sync run {report.run_id}: {report.inserted} loaded, {report.skipped} up to date")
        for mid, reason in sorted(report.rejected.items()):
            print(f"rejected {mid}: {reason}")
    return 1 if report.rejected else 0


def _export(settings: Settings, out: Path) -> int:
    manifest = write_export(build_public_reader(settings), out)["manifest"]
    assert isinstance(manifest, dict)
    print(f"export written: {out} ({len(manifest['heads'])} matches, digest {manifest['digest'][:16]})")
    return 0


def _silence_stdout() -> None:
    """Point stdout at /dev/null so the interpreter's final flush can't raise again."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.close(devnull)


def _exists(settings: Settings, match_id: str) -> bool:
    try:
        build_store(settings).load(match_id)
    except DuelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
