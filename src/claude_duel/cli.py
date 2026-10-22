"""`duel` command line: run, resume, replay."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from claude_duel.app.container import build_event_log, build_store
from claude_duel.app.replay import replay
from claude_duel.app.session import play
from claude_duel.config import Settings, load_settings
from claude_duel.domain.errors import DuelError
from claude_duel.domain.models import MatchState
from claude_duel.observability import InMemoryMetrics, configure_logging, log

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
    replay_p = sub.choices["replay"]
    replay_p.add_argument("--transcript", action="store_true")
    return p


def _summary(state: MatchState) -> str:
    board = ", ".join(f"{k}={v}" for k, v in sorted(state.scores.items()))
    u = state.usage
    return (
        f"match {state.match_id}: {state.stage.value} after round {state.round}: {board} "
        f"[calls={u.calls} tokens={u.tokens} est_usd={u.usd:.4f}]"
    )


def _run(settings: Settings, match_id: str | None) -> int:
    metrics = InMemoryMetrics()
    mid = match_id or uuid.uuid4().hex[:12]
    try:
        print(_summary(play(settings, mid, metrics)))
    finally:
        log(_LOG, logging.INFO, "metrics", match_id=mid, **metrics.summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        configure_logging(settings.log_level)
        if args.cmd == "run":
            return _run(settings, args.match_id)
        if args.cmd == "resume":
            return _run(settings, args.match_id) if _exists(settings, args.match_id) else 2
        report = replay(build_store(settings).load(args.match_id), build_event_log(settings).read(args.match_id))
        if args.transcript:
            print("\n".join(report.transcript))
        print(
            f"replay consistent={report.consistent} recomputed={report.recomputed} recorded={report.recorded}"
            + (f" mismatches={report.mismatches}" if report.mismatches else "")
        )
        return 0 if report.consistent else 1
    except DuelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _exists(settings: Settings, match_id: str) -> bool:
    try:
        build_store(settings).load(match_id)
    except DuelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
