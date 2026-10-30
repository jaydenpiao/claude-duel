"""`duel sync` / `duel export` against a real Postgres (ADR 0012).

Needs DUEL_TEST_DATABASE_URL: a superuser URL for a throwaway server (e.g. postgres:17 in Docker).
Each test gets a fresh database owned by a non-superuser role, so RLS and grants are really enforced.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.postgres import PostgresResults, PublicReader, connect, migrate, require_current
from claude_duel.cli import main
from claude_duel.domain.errors import ConfigError, ResultsDbError
from claude_duel.domain.flags import assert_no_flag, flag_of

ADMIN = os.environ.get("DUEL_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN, reason="DUEL_TEST_DATABASE_URL not set")
OWNER_PW = "owner-test-only"
EXPORTER_PW = "exporter-test-only"
INGEST_PW = "ingest-test-only"


def _url(dbname: str, user: str, password: str) -> str:
    return make_conninfo(ADMIN, dbname=dbname, user=user, password=password)


def _new_db() -> str:
    name = f"duel_t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        if admin.execute("select 1 from pg_roles where rolname = 'duel_test_owner'").fetchone() is None:
            admin.execute(f"create role duel_test_owner login createrole password '{OWNER_PW}'")
        admin.execute(f'create database "{name}" owner duel_test_owner')
    return name


def _drop(name: str) -> None:
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute(f'drop database "{name}" with (force)')


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    name = _new_db()
    owner = _url(name, "duel_test_owner", OWNER_PW)
    with connect(owner) as conn:
        migrate(conn)
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute(f"alter role duel_exporter login password '{EXPORTER_PW}'")
        if admin.execute("select 1 from pg_roles where rolname = 'duel_test_ingest'").fetchone() is None:
            admin.execute(f"create role duel_test_ingest login password '{INGEST_PW}'")
        admin.execute("grant duel_ingest to duel_test_ingest")
    monkeypatch.setenv("DUEL_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    monkeypatch.setenv("DUEL_ROUNDS", "2")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", _url(name, "duel_test_ingest", INGEST_PW))
    monkeypatch.setenv("MIGRATE_DATABASE_URL", owner)
    monkeypatch.setenv("EXPORT_DATABASE_URL", _url(name, "duel_exporter", EXPORTER_PW))
    yield {
        "owner": owner,
        "ingest": os.environ["DATABASE_URL"],
        "exporter": os.environ["EXPORT_DATABASE_URL"],
        "name": name,
    }
    _drop(name)


def _flags(store_dir: Path) -> list[str]:
    store = FileMatchStore(store_dir)
    out = []
    for path in store_dir.glob("*.secret"):
        state = store.load(path.name.removesuffix(".secret"))  # the test may read secrets; sync must not
        out += [state.secret] + [flag_of(state.secret, c, e) for c in state.challenges for e in range(4)]
    return out


def test_sync_export_never_leaks_a_secret(
    db: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for mid in ("p1", "p2"):
        assert main(["run", "--match-id", mid]) == 0
    assert main(["sync", "--boot", "5", "--format", "json"]) == 0
    first = capsys.readouterr().out.splitlines()[-1]
    assert json.loads(first)["inserted"] == 2, first
    assert main(["sync", "--boot", "5", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == 2  # second run inserts nothing
    out = tmp_path / "public.json"
    assert main(["export", "--out", str(out)]) == 0
    assert "2 matches" in capsys.readouterr().out
    export = out.read_text()
    with connect(db["owner"]) as conn:
        stored = json.dumps([dict(r) for r in conn.execute("select * from duel.events")], default=str)
    flags = _flags(tmp_path / "store")
    assert flags
    for flag in flags:
        assert_no_flag(export, flag, "export")
        assert_no_flag(stored, flag, "duel.events")
    data = json.loads(export)
    assert len(data["tables"]["matches"]) == 2
    assert {r["elo_order"] for r in data["tables"]["leaderboard"]} == {"match_id ascending"}


def test_rebuild_and_rewritten_history(db: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--match-id", "r1"]) == 0
    assert main(["sync", "--boot", "3"]) == 0
    assert main(["sync", "--rebuild", "--boot", "3"]) == 0
    assert "1 loaded" in capsys.readouterr().out
    with connect(db["owner"]) as conn:
        conn.execute(
            "insert into duel.events (event_hash, prev_hash, match_id, seq, kind, v, data)"
            " values (%s, %s, 'r1', 9999, 'x', 1, '{}')",
            ["f" * 64, "0" * 64],
        )
    capsys.readouterr()
    assert main(["sync", "--boot", "3"]) == 2  # halts loudly: a stored hash vanished from the log
    assert "r1" in capsys.readouterr().err


def test_exporter_is_read_only_and_views_only(db: dict[str, str]) -> None:
    with psycopg.connect(db["exporter"], autocommit=True) as conn:
        for stmt in (
            "select * from duel.events",
            "select rules from duel.matches",
            "insert into duel.sync_runs (id) values (repeat('a', 32))",
            "delete from duel.matches",
            "create table public_v.x (a int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
        assert conn.execute("select count(*) from public_v.matches").fetchone() == (0,)


def test_public_reader_refuses_writer_roles(db: dict[str, str]) -> None:
    with connect(db["owner"]) as conn, pytest.raises(ConfigError, match="export role"):
        PublicReader(conn)
    with connect(ADMIN) as conn, pytest.raises(ConfigError):
        PublicReader(conn)


def test_schema_hardening(db: dict[str, str]) -> None:
    with connect(db["owner"]) as conn:
        assert migrate(conn) == []  # idempotent
        tables = conn.execute(
            "select relname, relrowsecurity, relforcerowsecurity from pg_class c"
            " join pg_namespace n on n.oid = c.relnamespace where nspname = 'duel' and relkind = 'r'"
        ).fetchall()
        assert tables
        assert all(t["relrowsecurity"] and t["relforcerowsecurity"] for t in tables)
        views = conn.execute(
            "select relname, reloptions from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where nspname = 'public_v' and relkind = 'v'"
        ).fetchall()
        assert views
        assert all("security_invoker=true" in (v["reloptions"] or []) for v in views)
        exporter = conn.execute(
            "select rolsuper, rolcreaterole, rolbypassrls, pg_has_role('duel_exporter', 'duel_writer', 'MEMBER') w"
            " from pg_roles where rolname = 'duel_exporter'"
        ).fetchone()
        assert exporter == {"rolsuper": False, "rolcreaterole": False, "rolbypassrls": False, "w": False}
        definers = conn.execute(
            "select count(*) as n from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where nspname in ('duel', 'public_v') and prosecdef"
        ).fetchone()
        assert definers == {"n": 0}
        acl = conn.execute(
            "select defaclacl::text as acl from pg_default_acl"
            " where defaclnamespace = 0 and defaclobjtype = 'f' and defaclrole = current_user::regrole"
        ).fetchone()
        assert acl is not None
        assert "=X/" not in acl["acl"].replace("duel_test_owner=X/", "")  # PUBLIC has no default EXECUTE


def test_missing_urls_and_bad_url_never_echo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATE_DATABASE_URL", raising=False)
    monkeypatch.delenv("EXPORT_DATABASE_URL", raising=False)
    assert main(["sync"]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().err
    assert main(["export"]) == 2
    assert "EXPORT_DATABASE_URL is not set" in capsys.readouterr().err
    bad = make_conninfo(ADMIN, password="wrong-pw-xyz", user="nobody_here")
    with pytest.raises(ConfigError) as err:
        connect(bad)
    assert "wrong-pw-xyz" not in str(err.value)
    assert conninfo_to_dict(bad)["password"] == "wrong-pw-xyz"


def test_inconsistent_log_is_rejected_and_recorded(
    db: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--match-id", "bad"]) == 0
    (tmp_path / "store" / "bad.events.jsonl").write_text("not json\n")
    capsys.readouterr()
    assert main(["sync", "--boot", "3"]) == 1
    assert "rejected bad:" in capsys.readouterr().out
    with connect(db["owner"]) as conn:
        assert [r["match_id"] for r in conn.execute("select match_id from duel.sync_rejects")] == ["bad"]


def test_ingest_role_cannot_delete_and_runs_are_recorded(
    db: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--match-id", "g1"]) == 0
    assert main(["sync", "--boot", "3"]) == 0
    assert main(["sync", "--boot", "3"]) == 0
    with psycopg.connect(db["ingest"], autocommit=True) as conn:
        for stmt in ("delete from duel.matches", "truncate duel.events", "drop table duel.events"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
    with connect(db["owner"]) as conn:
        runs = conn.execute("select status, finished_at, git_sha, inserted, skipped from duel.sync_runs").fetchall()
        assert [(r["status"], r["inserted"], r["skipped"]) for r in runs] == [("ok", 1, 0), ("ok", 0, 1)]
        assert all(r["finished_at"] is not None for r in runs)
        snaps = conn.execute("select count(distinct sync_run_id) as n from duel.rating_snapshots").fetchone()
        assert snaps == {"n": 1}  # the second run had the same inputs: no duplicate snapshot
        seen = conn.execute("select first_synced_at from duel.match_first_seen").fetchall()
    capsys.readouterr()
    assert main(["sync", "--rebuild", "--boot", "3", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["inserted"] == 1
    with connect(db["owner"]) as conn:
        assert conn.execute("select first_synced_at from duel.match_first_seen").fetchall() == seen
        assert conn.execute("select count(*) as n from duel.sync_runs").fetchone() == {"n": 3}
        conn.execute("update duel.matches set rejected = true")
    with psycopg.connect(db["exporter"], autocommit=True) as conn:
        assert conn.execute("select count(*) from public_v.matches").fetchone() == (0,)
        assert conn.execute("select count(*) from public_v.match_agents").fetchone() == (0,)
        assert conn.execute("select count(*) from public_v.leaderboard").fetchone() != (0,)


def test_sync_refuses_superuser_and_pending_migrations(
    db: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", make_conninfo(ADMIN, dbname=db["name"]))
    assert main(["sync"]) == 2
    assert "superuser" in capsys.readouterr().err
    fresh = _new_db()
    try:
        owner = _url(fresh, "duel_test_owner", OWNER_PW)
        monkeypatch.setenv("DATABASE_URL", owner)
        monkeypatch.setenv("MIGRATE_DATABASE_URL", owner)
        assert main(["sync"]) == 2
        assert "duel db migrate" in capsys.readouterr().err
        assert main(["db", "migrate"]) == 0
        assert "0001_results.sql" in capsys.readouterr().out
        assert main(["db", "migrate"]) == 0
        assert "up to date" in capsys.readouterr().out
        assert main(["sync"]) == 2  # the owner may migrate and --rebuild, never routine sync
        assert "table owner" in capsys.readouterr().err
        with connect(owner) as conn:
            conn.execute("update duel.schema_migrations set sha256 = repeat('0', 64)")
            with pytest.raises(ConfigError, match="forward-only"):
                require_current(conn)
            with pytest.raises(ConfigError, match="forward-only"):
                migrate(conn)
    finally:
        _drop(fresh)


def test_concurrent_migrations_apply_once() -> None:
    fresh = _new_db()
    try:
        owner = _url(fresh, "duel_test_owner", OWNER_PW)
        with ThreadPoolExecutor(2) as pool:
            done = list(pool.map(lambda _: migrate(connect(owner)), range(2)))
        assert sorted(len(d) for d in done) == [0, 1]
    finally:
        _drop(fresh)


def test_security_definer_functions_are_refused(db: dict[str, str]) -> None:
    with connect(db["owner"]) as conn:
        conn.execute(
            "create function public_v.leak() returns text language sql security definer"
            " as 'select string_agg(rules::text, '','') from duel.matches'"
        )
        with pytest.raises(ConfigError, match="SECURITY DEFINER"):
            require_current(conn)
    with connect(db["exporter"]) as conn, pytest.raises(ConfigError, match="SECURITY DEFINER"):
        PublicReader(conn)


def test_database_errors_are_redacted(db: dict[str, str]) -> None:
    with connect(db["ingest"]) as conn:
        results = PostgresResults(conn)
        with pytest.raises(ResultsDbError) as err:
            results.start_run("not-a-uuid-secret-value", None)
    assert "secret-value" not in str(err.value)
    assert "CheckViolation" in str(err.value)
