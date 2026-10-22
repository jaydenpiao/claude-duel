"""`duel` command line: run, resume, replay, card, estimate, series, ratings, tournament."""

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

from claude_duel.app.budget import check_model_allowed
from claude_duel.app.container import build_event_log, build_ledger, build_store
from claude_duel.app.estimate import estimate, render
from claude_duel.app.replay import replay
from claude_duel.app.series import play_series, verify_series
from claude_duel.app.session import play
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings, load_settings
from claude_duel.domain.errors import ConfigError, DuelError
from claude_duel.domain.events import fold
from claude_duel.domain.models import MatchState
from claude_duel.domain.modes import MODES, preset
from claude_duel.domain.ratings import ratings, results
from claude_duel.domain.scoring import match_stats
from claude_duel.domain.series import SeriesRule
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
    tour = sub.add_parser("tournament", help="play or resume the config's [tournament] (round robin or Swiss)")
    tour.add_argument("--tournament-id", help="explicit id (re-running an existing id resumes it)")
    tour.add_argument("--format", choices=("text", "json"), default="text")
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
    limit, lifetime = settings.budget.key_limit_usd, build_ledger(settings).total_usd
    if args.view == "arena" or (args.view == "auto" and sys.stdout.isatty()):
        return ArenaView(Console(), mid, limit, lifetime, pace=max(args.pace, 0.0))
    return Commentary(sys.stdout, mid, limit, lifetime)


def _run(settings: Settings, match_id: str | None, args: argparse.Namespace) -> int:
    metrics = InMemoryMetrics()
    mid = match_id or uuid.uuid4().hex[:12]
    observer = _observer(settings, mid, args)
    try:
        state = play(settings, mid, metrics, observer)
    finally:
        if isinstance(observer, ArenaView):
            observer.close()
        log(_LOG, logging.INFO, "metrics", match_id=mid, **metrics.summary())
    if observer is None:
        print(_summary(state))
        return 0
    report = replay(build_store(settings).load(mid), build_event_log(settings).read(mid))
    print(f"replay verified {'✓' if report.consistent else '✗ ' + ', '.join(report.mismatches)}")
    return 0 if report.consistent else 1


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
    }
    if args.cmd in handlers:
        return handlers[args.cmd]()
    report = replay(build_store(settings).load(args.match_id), build_event_log(settings).read(args.match_id))
    if args.transcript:
        print("\n".join(report.transcript))
    print(
        f"replay consistent={report.consistent} recomputed={report.recomputed} recorded={report.recorded}"
        + (f" mismatches={report.mismatches}" if report.mismatches else "")
    )
    return 0 if report.consistent else 1


def _estimate(settings: Settings, mode: str | None, model: str | None) -> int:
    chosen = model or settings.openai_model
    check_model_allowed(chosen)
    try:
        names = [mode] if mode else sorted(MODES)
        rows = [estimate(preset(n), chosen, settings.budget) for n in names]
    except KeyError as exc:
        raise ConfigError(str(exc.args[0])) from exc
    print(render(rows))
    return 0


def _series(settings: Settings, series_id: str, games: int, fmt: str) -> int:
    try:
        rule = SeriesRule(games=games)
    except ValueError as exc:
        raise ConfigError(f"bad series: {exc}") from exc
    metrics = InMemoryMetrics()
    state = play_series(settings, series_id, rule, metrics)
    report = verify_series(settings, series_id)
    winner = state.winner()
    if fmt == "json":
        out = {
            "series_id": series_id,
            "best_of": state.rule.games,
            "wins": state.wins(),
            "winner": winner,
            "games": [g.model_dump() for g in state.games],
            "verified": report.consistent,
        }
        print(json.dumps(out))
    else:
        for g in state.games:
            print(f"game {g.game_no} ({g.match_id}): {g.winner or 'draw'} · {g.scores}")
        result = "undecided" if not state.done() else f"winner: {winner}" if winner else "drawn"
        print(f"series {series_id} best of {state.rule.games}: {result} · wins {state.wins()}")
        print(f"series verified {'✓' if report.consistent else '✗ ' + ', '.join(report.mismatches)}")
    return 0 if report.consistent and state.done() else 1


def _ratings(settings: Settings, boot: int, fmt: str) -> int:
    log = build_event_log(settings)
    table = ratings(results(fold(mid, log.read(mid)) for mid in log.ids()), boot=boot)
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
    state = play_tournament(settings, tournament_id, settings.tournament, InMemoryMetrics())
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
