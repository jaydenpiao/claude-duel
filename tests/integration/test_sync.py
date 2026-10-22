"""app/sync.py against an in-memory ResultsStore: verify, replay, idempotency, halt on rewritten history."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.app.engine import MatchEngine
from claude_duel.app.sync import ELO_ORDER, sync
from claude_duel.domain.errors import HistoryRewrittenError
from claude_duel.domain.models import MatchState
from claude_duel.ports import Row


class MemoryResults:
    def __init__(self) -> None:
        self.tables: dict[str, list[Row]] = {}
        self.rejects: list[tuple[int, str, str]] = []
        self.runs: list[tuple[int, dict[str, int], list[Row]]] = []

    def stored_hashes(self, match_id: str) -> set[str]:
        return {str(r["event_hash"]) for r in self.tables.get("events", []) if r["match_id"] == match_id}

    def ingest(self, tables: dict[str, list[Row]]) -> int:
        have = self.stored_hashes(str(tables["matches"][0]["match_id"]))
        new = [r for r in tables["events"] if r["event_hash"] not in have]
        for name, rows in tables.items():
            self.tables.setdefault(name, []).extend(new if name == "events" else rows)
        return len(new)

    def start_run(self) -> int:
        return len(self.runs) + 1

    def reject(self, run_id: int, match_id: str, reason: str) -> None:
        self.rejects.append((run_id, match_id, reason))

    def finish_run(self, run_id: int, counts: dict[str, int], ratings: list[Row]) -> None:
        self.runs.append((run_id, counts, ratings))

    def clear(self) -> None:
        self.tables.clear()


Engine = Callable[..., MatchEngine]
State = Callable[..., MatchState]


def _two(make_engine: Engine, new_state: State) -> None:
    make_engine().run(new_state("m1"))
    make_engine().run(new_state("m2"))


def test_sync_is_idempotent_and_never_needs_secrets(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore, store_root: Path
) -> None:
    _two(make_engine, new_state)
    for secret in store_root.rglob("*.secret"):
        secret.unlink()  # sync must work with no secret on disk at all
    db = MemoryResults()
    first = sync(event_log, store, db, boot=5)
    assert (first.inserted, first.skipped, first.rejected) == (2, 0, {})
    again = sync(event_log, store, db, boot=5)
    assert (again.inserted, again.skipped) == (0, 2)
    _, counts, ratings = db.runs[-1]
    assert counts == {"inserted": 0, "skipped": 2, "rejected": 0}
    assert {r["elo_order"] for r in ratings} == {ELO_ORDER}
    assert len(ratings) == 2


def test_vanished_hash_halts(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore
) -> None:
    _two(make_engine, new_state)
    db = MemoryResults()
    sync(event_log, store, db, boot=5)
    db.tables["events"].append({"match_id": "m1", "event_hash": "f" * 64})
    with pytest.raises(HistoryRewrittenError, match="m1"):
        sync(event_log, store, db, boot=5)


def test_broken_or_mismatched_logs_are_rejected(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore, store_root: Path
) -> None:
    _two(make_engine, new_state)
    log = next(store_root.rglob("m1*.jsonl"))
    lines = log.read_text().splitlines()
    first = json.loads(lines[0])
    first["event"]["seq"] = 7
    log.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
    snap = next(p for p in store_root.rglob("m2*") if p.suffix == ".json")
    snap.unlink()
    db = MemoryResults()
    report = sync(event_log, store, db, boot=5)
    assert set(report.rejected) == {"m1", "m2"}
    assert {m for _, m, _ in db.rejects} == {"m1", "m2"}
    assert "events" not in db.tables
