"""One ``ResultsStore`` contract for every store: memory and postgres (psycopg) (ADR 0012).

postgres needs DUEL_TEST_DATABASE_URL (a throwaway superuser URL).
Each SQL case gets a fresh database migrated by a non-superuser owner.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest

from claude_duel.adapters.store import results_sql
from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.results_sql import Executor, SqlResults
from claude_duel.app.engine import MatchEngine
from claude_duel.app.sync import ELO_ORDER, rows, sync
from claude_duel.domain.errors import ResultsDbError
from claude_duel.domain.models import MatchState
from claude_duel.domain.results import MatchProjection, RatingRow, RunCounts
from claude_duel.ports import ResultsStore
from tests.memory_results import MemoryResults

ADMIN = os.environ.get("DUEL_TEST_DATABASE_URL", "")
PW = "owner-test-only"  # the e2e owner: it created the cluster-wide duel_* roles


def _fresh_db() -> tuple[str, str]:
    import psycopg  # noqa: PLC0415 - only when a SQL transport runs
    from psycopg.conninfo import make_conninfo  # noqa: PLC0415

    name = f"duel_c_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        if admin.execute("select 1 from pg_roles where rolname = 'duel_test_owner'").fetchone() is None:
            admin.execute(f"create role duel_test_owner login createrole password '{PW}'")
        admin.execute(f'create database "{name}" owner duel_test_owner')
    return name, make_conninfo(ADMIN, dbname=name, user="duel_test_owner", password=PW)


def _drop(name: str) -> None:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute(f'drop database "{name}" with (force)')


@pytest.fixture(
    params=[
        "memory",
        pytest.param("postgres", marks=pytest.mark.skipif(not ADMIN, reason="DUEL_TEST_DATABASE_URL not set")),
    ]
)
def backend(request: pytest.FixtureRequest) -> Iterator[tuple[str, ResultsStore, Executor | None]]:
    if request.param == "memory":
        yield "memory", MemoryResults(), None
        return
    name, url = _fresh_db()
    try:
        from claude_duel.adapters.store.postgres import PgExecutor, connect  # noqa: PLC0415

        with connect(url) as conn:
            ex: Executor = PgExecutor(conn)
            results_sql.migrate(ex)
            yield "postgres", SqlResults(ex), ex
    finally:
        _drop(name)


@pytest.fixture
def projections(
    make_engine: Callable[..., MatchEngine],
    new_state: Callable[..., MatchState],
    event_log: FileEventLog,
    store: FileMatchStore,
) -> dict[str, MatchProjection]:
    out = {}
    for mid in ("m1", "m2"):
        make_engine().run(new_state(mid))
        out[mid] = rows(store.snapshot(mid), event_log.chain(mid))
    return out


def test_ingest_is_atomic_and_idempotent(
    backend: tuple[str, ResultsStore, Executor | None], projections: dict[str, MatchProjection]
) -> None:
    _, db, _ = backend
    p = projections["m1"]
    assert db.stored("m1").hashes == frozenset()
    assert db.ingest(p) == len(p.events)
    assert db.ingest(p) == 0
    got = db.stored("m1")
    assert got.hashes == {e.event_hash for e in p.events}
    assert (got.projection_version, got.rejected) == (p.match.projection_version, False)


def test_reject_hides_until_reingested(
    backend: tuple[str, ResultsStore, Executor | None], projections: dict[str, MatchProjection]
) -> None:
    _, db, _ = backend
    db.start_run("11111111-1111-4111-8111-111111111111", None)
    db.ingest(projections["m2"])
    db.reject("11111111-1111-4111-8111-111111111111", "m2", "ReplayMismatchError")
    assert db.stored("m2").rejected
    db.ingest(projections["m2"])
    assert not db.stored("m2").rejected


def test_run_lifecycle_and_ratings_digest(backend: tuple[str, ResultsStore, Executor | None]) -> None:
    _, db, _ = backend
    assert db.last_ratings_digest() is None
    rating = RatingRow("e1", 1, 1500.0, 0.0, -1.0, 1.0, ELO_ORDER, 16.0, "m1")
    run = "22222222-2222-4222-8222-222222222222"
    db.start_run(run, "abc123")
    db.finish_run(run, "ok", RunCounts(1, 0, 0), [rating], "d" * 64)
    assert db.last_ratings_digest() == "d" * 64
    failed = "33333333-3333-4333-8333-333333333333"
    db.start_run(failed, None)
    db.finish_run(failed, "failed", RunCounts(), [], "e" * 64)
    assert db.last_ratings_digest() == "d" * 64  # only ok runs count


def test_sync_end_to_end(
    backend: tuple[str, ResultsStore, Executor | None],
    make_engine: Callable[..., MatchEngine],
    new_state: Callable[..., MatchState],
    event_log: FileEventLog,
    store: FileMatchStore,
) -> None:
    _, db, _ = backend
    make_engine().run(new_state("m1"))
    make_engine().run(new_state("m2"))
    first = sync(event_log, store, db, boot=5)
    assert (first.inserted, first.skipped, first.rejected) == (2, 0, {})
    again = sync(event_log, store, db, boot=5)
    assert (again.inserted, again.skipped, again.ratings_written) == (0, 2, False)


def test_rebuild(backend: tuple[str, ResultsStore, Executor | None], projections: dict[str, MatchProjection]) -> None:
    _, db, _ = backend
    db.ingest(projections["m1"])
    with db.rebuilding():
        db.ingest(projections["m1"])
    assert db.stored("m1").hashes  # the append-only history survives either way


def test_errors_are_redacted(backend: tuple[str, ResultsStore, Executor | None]) -> None:
    _, _, ex = backend
    if ex is None:
        pytest.skip("memory has no SQL")
    secret = "hunter2-row-value"
    with pytest.raises(ResultsDbError) as err:
        ex.query("select (%s)::int", [secret])
    assert secret not in str(err.value)
    assert err.value.sqlstate == "22P02"
    with pytest.raises(ResultsDbError):
        ex.batch([("select 1", []), ("select 1/0", [])])
