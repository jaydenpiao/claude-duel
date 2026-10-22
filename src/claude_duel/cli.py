"""`duel` command line: run, resume, replay."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from claude_duel.app.container import build_event_log, build_ledger, build_store
from claude_duel.app.replay import replay
from claude_duel.app.session import play
from claude_duel.config import Settings, load_settings
from claude_duel.domain.errors import DuelError
from claude_duel.domain.models import MatchState
from claude_duel.observability import InMemoryMetrics, configure_logging, log
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
        }
    )


def _run(settings: Settings, match_id: str | None, fmt: str) -> int:
    metrics = InMemoryMetrics()
    mid = match_id or uuid.uuid4().hex[:12]
    commentary = (
        Commentary(sys.stdout, mid, settings.budget.key_limit_usd, build_ledger(settings).total_usd)
        if fmt == "text"
        else None
    )
    try:
        state = play(settings, mid, metrics, commentary)
    finally:
        log(_LOG, logging.INFO, "metrics", match_id=mid, **metrics.summary())
    if commentary is None:
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
    if args.cmd == "run":
        return _run(settings, args.match_id, args.format)
    if args.cmd == "resume":
        return _run(settings, args.match_id, args.format) if _exists(settings, args.match_id) else 2
    report = replay(build_store(settings).load(args.match_id), build_event_log(settings).read(args.match_id))
    if args.transcript:
        print("\n".join(report.transcript))
    print(
        f"replay consistent={report.consistent} recomputed={report.recomputed} recorded={report.recorded}"
        + (f" mismatches={report.mismatches}" if report.mismatches else "")
    )
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
