"""`duel` command line: run, resume, replay."""

from __future__ import annotations

import argparse
import secrets
import sys
import uuid
from pathlib import Path

from claude_duel.app.container import build_engine
from claude_duel.app.replay import replay
from claude_duel.config import Settings, load_settings
from claude_duel.domain.errors import DuelError, MatchNotFoundError
from claude_duel.domain.models import MatchState, Stage
from claude_duel.domain.state_machine import recover
from claude_duel.observability import InMemoryMetrics, configure_logging


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
    engine, store = build_engine(settings, InMemoryMetrics())
    mid = match_id or uuid.uuid4().hex[:12]
    try:
        state = store.load(mid)
        if state.stage is Stage.FAILED:
            recover(state)
    except MatchNotFoundError:
        state = MatchState(
            match_id=mid,
            secret=secrets.token_hex(32),
            agents=settings.agents,
            rules=settings.rules,
            limits=settings.limits,
        )
        store.save(state)
    print(_summary(engine.run(state)))
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
        _, store = build_engine(settings, InMemoryMetrics())
        report = replay(store.load(args.match_id))
        if args.transcript:
            print("\n".join(report.transcript))
        print(f"replay consistent={report.consistent} recomputed={report.recomputed} recorded={report.recorded}")
        return 0 if report.consistent else 1
    except DuelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _exists(settings: Settings, match_id: str) -> bool:
    _, store = build_engine(settings, InMemoryMetrics())
    try:
        store.load(match_id)
    except DuelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
