"""app/sync.py against an in-memory ResultsStore: verify, replay, idempotency, halt on rewritten history."""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.app import sync as sync_mod
from claude_duel.app.engine import MatchEngine
from claude_duel.app.sync import ELO_ORDER, REASON_MAX, reason, sync
from claude_duel.domain.errors import DuelError, FlagLeakError, HistoryRewrittenError, ReplayMismatchError
from claude_duel.domain.models import Event, MatchState
from claude_duel.domain.results import MatchProjection
from tests.helpers import FLAG
from tests.memory_results import MemoryResults

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
    runs = list(db.runs.values())
    assert [r.status for r in runs] == ["ok", "ok"]
    assert {r.elo_order for r in runs[0].ratings} == {ELO_ORDER}
    assert len(runs[0].ratings) == 2
    assert runs[1].ratings == []  # same inputs: no duplicate snapshot
    assert not again.ratings_written


def test_vanished_hash_halts(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore
) -> None:
    _two(make_engine, new_state)
    db = MemoryResults()
    sync(event_log, store, db, boot=5)
    db.events["m1"].add("f" * 64)
    with pytest.raises(HistoryRewrittenError, match="m1"):
        sync(event_log, store, db, boot=5)
    assert list(db.runs.values())[-1].status == "halted"


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
    assert db.matches == {}


def test_corrupt_snapshot_and_bad_rows_are_rejects_not_crashes(
    make_engine: Engine,
    new_state: State,
    event_log: FileEventLog,
    store: FileMatchStore,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two(make_engine, new_state)
    snap = next(p for p in store_root.rglob("m1*") if p.suffix == ".json")
    snap.write_text("{not json")
    real = sync_mod._call_row

    def broken(mid: str, h: str, e: Event) -> object:
        if mid == "m2":
            raise KeyError("challenge")
        return real(mid, h, e)

    monkeypatch.setattr(sync_mod, "_call_row", broken)
    db = MemoryResults()
    report = sync(event_log, store, db, boot=5)
    assert set(report.rejected) == {"m1", "m2"}
    assert report.rejected["m1"].startswith("ValidationError")
    assert report.rejected["m2"].startswith("KeyError")
    assert next(iter(db.runs.values())).counts.rejected == 2


def test_replay_mismatch_has_its_own_error(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore
) -> None:
    make_engine().run(new_state("m1"))
    state = store.load("m1")
    store.save(state.model_copy(update={"round": state.round + 5}))
    with pytest.raises(ReplayMismatchError, match="replay mismatch"):
        sync_mod._verified(event_log, store, "m1")


def test_reasons_are_scrubbed_and_capped() -> None:
    assert FLAG not in reason(ValueError(f"leak {FLAG}"))
    assert reason(ValueError(f"input {FLAG}")) == "ValueError"  # non-domain errors: class name only
    long = reason(DuelError("x" * 1000))
    assert len(long) == REASON_MAX
    assert long.endswith("…")


def test_unexpected_error_marks_the_run_failed(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore
) -> None:
    _two(make_engine, new_state)

    class Boom(MemoryResults):
        def ingest(self, projection: MatchProjection) -> int:
            raise RuntimeError("db gone")

        def finish_run(self, *args: object, **kw: object) -> None:
            if args[1] != "failed":
                raise AssertionError
            super().finish_run(*args, **kw)  # type: ignore[arg-type]

    db = Boom()
    with pytest.raises(RuntimeError):
        sync(event_log, store, db, boot=5)
    assert [r.status for r in db.runs.values()] == ["failed"]


def test_failing_to_record_failure_keeps_the_original_error(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore
) -> None:
    make_engine().run(new_state("m1"))

    class Gone(MemoryResults):
        def ingest(self, projection: MatchProjection) -> int:
            raise RuntimeError("db gone")

        def finish_run(self, *args: object, **kw: object) -> None:
            raise OSError("connection closed")

    with pytest.raises(RuntimeError, match="db gone"):
        sync(event_log, store, Gone(), boot=5, git_sha="abc")


def test_reprojection_rebuild_and_unreject(
    make_engine: Engine,
    new_state: State,
    event_log: FileEventLog,
    store: FileMatchStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two(make_engine, new_state)
    db = MemoryResults()
    sync(event_log, store, db, boot=5)
    monkeypatch.setattr(sync_mod, "PROJECTION_VERSION", 2)
    bumped = sync(event_log, store, db, boot=5)
    assert bumped.inserted == 2  # a new projection version re-projects every match
    assert bumped.ratings_written  # and the digest covers the version
    db.reject("x", "m1", "earlier")
    healed = sync(event_log, store, db, boot=5)
    assert (healed.inserted, healed.skipped) == (1, 1)
    assert "m1" not in db.rejected
    first_seen = dict(db.first_seen)
    rebuilt = sync(event_log, store, db, boot=5, rebuild=True)
    assert (rebuilt.inserted, rebuilt.skipped) == (2, 0)
    assert db.first_seen == first_seen
    assert not rebuilt.ratings_written


def test_failed_rebuild_restores_previous_data(
    make_engine: Engine, new_state: State, event_log: FileEventLog, store: FileMatchStore
) -> None:
    _two(make_engine, new_state)
    db = MemoryResults()
    sync(event_log, store, db, boot=5)
    before = set(db.matches)
    with pytest.raises(RuntimeError), db.rebuilding():
        raise RuntimeError
    assert set(db.matches) == before


@pytest.mark.parametrize(
    "encode", [lambda f: codecs.encode(f, "rot13"), lambda f: " ".join(str(b) for b in f.encode())]
)
def test_legacy_log_with_flag_shaped_event_is_rejected(
    make_engine: Engine,
    new_state: State,
    event_log: FileEventLog,
    store: FileMatchStore,
    encode: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_engine().run(new_state("m1"))
    chain = list(event_log.chain("m1"))
    clean = list(chain)
    h, last = chain[-1]
    chain[-1] = (h, last.model_copy(update={"data": {**last.data, "note": encode("DUEL{" + "ab12" * 4 + "}")}}))

    class Legacy(FileEventLog):
        def chain(self, match_id: str) -> list[tuple[str, Event]]:
            return chain

    real = sync_mod.replay
    monkeypatch.setattr(sync_mod, "replay", lambda state, events: real(state, [e for _, e in clean]))
    with pytest.raises(FlagLeakError, match="flag-shaped"):
        sync_mod._verified(Legacy(event_log._root), store, "m1")
