"""Postgres results index (ADR 0012): migrations, idempotent ingest and the read-only public export.

Connection URLs come only from the environment and are never logged or put in an error message.
``migrate`` and ``--rebuild`` need the schema owner (``MIGRATE_DATABASE_URL``); routine ``duel sync`` should run as
a ``duel_ingest`` login (``DATABASE_URL``), which cannot delete or truncate. ``PublicReader`` refuses any role that
can write.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from claude_duel.domain.errors import ConfigError, ResultsDbError
from claude_duel.domain.results import (
    AgentRow,
    AttackRow,
    CallRow,
    EntrantRow,
    EventRow,
    MatchProjection,
    MatchRow,
    RatingRow,
    ResultRow,
    RunCounts,
    RunStatus,
    ScoreRow,
    Stored,
)

PUBLIC_VIEWS = ("matches", "match_agents", "leaderboard", "rating_history", "model_daily", "cost_daily")
LOCK_KEY = "duel.migrate"  # pg_advisory_xact_lock(hashtext(...)): one migrator at a time
# Projection row type -> (table, upsert key). A key means a re-run refreshes the row in place; None means the row is
# append-only (keyed on an event hash or entrant id) and a conflict is skipped.
_TABLE: dict[type, tuple[str, str | None]] = {
    MatchRow: ("matches", "match_id"),
    EntrantRow: ("entrants", None),
    AgentRow: ("match_agents", "match_id, agent_name"),
    ResultRow: ("match_results", "match_id"),
    EventRow: ("events", None),
    CallRow: ("calls", None),
    AttackRow: ("attacks", None),
    ScoreRow: ("round_scores", None),
}
# What `--rebuild` empties: everything derived from the logs. Run history, rating snapshots, rejects and
# first-seen times are not derivable, so they are kept.
_DERIVED = ("matches", "entrants", "match_agents", "match_results", "events", "calls", "attacks", "round_scores")

Conn = psycopg.Connection[dict[str, Any]]


_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})
_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})


def require_tls(url: str, env: Mapping[str, str] | None = None) -> None:
    """Refuse a non-local host unless ``sslmode`` is require or stricter (libpq's default ``prefer`` downgrades).

    Loopback hosts and unix sockets (``/path`` hosts) are exempt. Neither the URL nor libpq's message is echoed.
    """
    env = os.environ if env is None else env
    try:
        info = conninfo_to_dict(url)
    except psycopg.Error:
        raise ConfigError("the results database URL is malformed") from None
    hosts = str(info.get("host") or env.get("PGHOST", "")).split(",")
    remote = [h for h in hosts if h.strip() not in _LOCAL_HOSTS and not h.startswith("/")]
    mode = str(info.get("sslmode") or env.get("PGSSLMODE", "prefer"))
    if remote and mode not in _TLS_MODES:
        raise ConfigError("a remote results database needs sslmode=require, verify-ca or verify-full in its URL")


def connect(url: str) -> Conn:
    require_tls(url)
    try:
        return psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=15)
    except psycopg.Error as exc:  # the message can name host and user; keep it out of output
        raise ConfigError(f"cannot connect to the results database ({type(exc).__name__})") from None


@dataclasses.dataclass(frozen=True)
class RoleFacts:
    """What the connected role can do, from one ``pg_roles`` query (quality L25)."""

    superuser: bool
    createrole: bool
    bypassrls: bool
    writer: bool  # member of duel_writer
    ingest: bool  # member of duel_ingest
    owner: bool  # owns a table in schema duel
    reader: bool  # member of duel_reader


_ROLE_FACTS = (
    "select r.rolsuper as superuser, r.rolcreaterole as createrole, r.rolbypassrls as bypassrls,"
    " pg_has_role(current_user, 'duel_writer', 'MEMBER') as writer,"
    " pg_has_role(current_user, 'duel_ingest', 'MEMBER') as ingest,"
    " exists (select from pg_tables where schemaname = 'duel' and tableowner = current_user) as owner,"
    " pg_has_role(current_user, 'duel_reader', 'MEMBER') as reader"
    " from pg_roles r where r.rolname = current_user"
)


def role_facts(conn: Conn) -> RoleFacts:
    row = conn.execute(_ROLE_FACTS).fetchone()
    if row is None:
        raise ConfigError("cannot read the current role from pg_roles")
    return RoleFacts(**{f.name: bool(row[f.name]) for f in dataclasses.fields(RoleFacts)})


def refuse_superuser(conn: Conn) -> None:
    """``duel sync`` must not run as a role that ignores row-level security (superuser or BYPASSRLS)."""
    facts = role_facts(conn)
    if facts.superuser or facts.bypassrls:
        raise ConfigError("refusing to sync as a superuser or BYPASSRLS role: use a duel_ingest or owner login")


def refuse_table_owner(conn: Conn) -> None:
    """Routine ``duel sync`` must not run as a role that owns a ``duel`` table (it could truncate or drop it);
    the owner is for ``duel db migrate`` and ``--rebuild`` only."""
    if role_facts(conn).owner:
        raise ConfigError("refusing to sync as a duel table owner: use a duel_ingest login (owner only for --rebuild)")


def refuse_definer_functions(conn: Conn) -> None:
    """Refuse a SECURITY DEFINER function in ``duel`` or ``public_v``: it would bypass RLS and column grants."""
    row = conn.execute(
        "select count(*) as n from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname in ('duel', 'public_v') and p.prosecdef"
    ).fetchone()
    if row is None or row["n"]:
        raise ConfigError("a SECURITY DEFINER function exists in schema duel or public_v; remove it first")


@contextmanager
def db_errors() -> Iterator[None]:
    """Re-raise a database error as its class and SQLSTATE only: server messages can quote row values."""
    try:
        yield
    except psycopg.Error as exc:
        raise ResultsDbError(
            f"results database error: {type(exc).__name__} ({exc.sqlstate or 'no sqlstate'})"
        ) from None


def migrations() -> list[tuple[str, str, str]]:
    """``(name, sha256, sql)`` for every bundled ``migrations/NNNN_*.sql``, in order."""
    files = sorted(resources.files(__package__).joinpath("migrations").iterdir(), key=lambda r: r.name)
    out = []
    for res in files:
        if res.name.endswith(".sql"):
            text = res.read_text(encoding="utf-8")
            out.append((res.name, hashlib.sha256(text.encode()).hexdigest(), text))
    return out


def check_applied(applied: dict[str, str]) -> list[str]:
    """Names still pending; raises if an applied migration was edited or is unknown (forward-only)."""
    bundled = {name: digest for name, digest, _ in migrations()}
    for name, digest in applied.items():
        if bundled.get(name) != digest:
            raise ConfigError(f"migration {name} was changed or removed after it was applied (forward-only)")
    return [name for name in bundled if name not in applied]


def migrate(conn: Conn) -> list[str]:
    """Apply pending migrations in one transaction under an advisory lock. Returns the names applied."""
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        conn.execute("create schema if not exists duel")
        conn.execute(
            "create table if not exists duel.schema_migrations"
            " (name text primary key, sha256 char(64) not null, applied_at timestamptz not null default now())"
        )
        pending = check_applied(_applied(conn))
        for name, digest, text in migrations():
            if name in pending:
                conn.execute(sql.SQL(text))  # our own bundled file, never input
                conn.execute("insert into duel.schema_migrations (name, sha256) values (%s, %s)", (name, digest))
    return pending


def require_current(conn: Conn) -> None:
    """Fail clearly unless every bundled migration is applied unchanged (``duel sync`` never migrates)."""
    try:
        with conn.transaction():
            applied = _applied(conn)
    except psycopg.errors.UndefinedTable:
        applied = {}
    refuse_definer_functions(conn)
    if check_applied(applied):
        raise ConfigError("the results database schema is behind: run `duel db migrate` (as the schema owner)")


def _applied(conn: Conn) -> dict[str, str]:
    return {r["name"]: r["sha256"] for r in conn.execute("select name, sha256 from duel.schema_migrations")}


def _value(v: object) -> object:
    return Jsonb(v) if isinstance(v, dict | list) else v


def _insert(table: str, cols: Sequence[str], key: str | None) -> sql.Composed:
    stmt = sql.SQL("insert into duel.{} ({}) values ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    if key is None:
        return stmt + sql.SQL(" on conflict do nothing")
    sets = sql.SQL(", ").join(sql.SQL("{0} = excluded.{0}").format(sql.Identifier(c)) for c in cols)
    stmt += sql.SQL(" on conflict ({}) do update set ").format(sql.SQL(key)) + sets
    if table == "matches":
        stmt += sql.SQL(", rejected = false, synced_at = now()")
    return stmt


def _columns(row: object) -> list[str]:
    return [f.name for f in dataclasses.fields(row)]  # type: ignore[arg-type]


class PostgresResults:
    """``ResultsStore`` on Postgres. Each match is one transaction; a table's rows go in one ``executemany``."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def stored(self, match_id: str) -> Stored:
        with db_errors():
            return self._stored(match_id)

    def _stored(self, match_id: str) -> Stored:
        row = self._conn.execute(
            "select projection_version, rejected from duel.matches where match_id = %s", (match_id,)
        ).fetchone()
        cur = self._conn.execute("select event_hash from duel.events where match_id = %s", (match_id,))
        hashes = frozenset(r["event_hash"] for r in cur)
        if row is None:
            return Stored(hashes)
        return Stored(hashes, row["projection_version"], row["rejected"])

    def ingest(self, projection: MatchProjection) -> int:
        p = projection
        groups: list[Sequence[object]] = [
            [p.match],
            p.entrants,
            p.agents,
            [p.result],
            p.events,
            p.calls,
            p.attacks,
            p.scores,
        ]
        new_events = 0
        with db_errors(), self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                "insert into duel.match_first_seen (match_id) values (%s) on conflict do nothing", (p.match.match_id,)
            )
            for rows in groups:
                if not rows:
                    continue
                table, key = _TABLE[type(rows[0])]
                cols = _columns(rows[0])
                cur.executemany(_insert(table, cols, key), [[_value(getattr(r, c)) for c in cols] for r in rows])
                if table == "events":
                    new_events = cur.rowcount
        return new_events

    def start_run(self, run_id: str, git_sha: str | None) -> None:
        with db_errors(), self._conn.transaction():
            self._conn.execute("insert into duel.sync_runs (id, git_sha) values (%s, %s)", (run_id, git_sha))

    def reject(self, run_id: str, match_id: str, reason: str) -> None:
        with db_errors(), self._conn.transaction():
            self._conn.execute(
                "insert into duel.sync_rejects (sync_run_id, match_id, reason) values (%s, %s, %s)"
                " on conflict do nothing",
                (run_id, match_id, reason),
            )
            self._conn.execute("update duel.matches set rejected = true where match_id = %s", (match_id,))

    def last_ratings_digest(self) -> str | None:
        with db_errors():
            row = self._conn.execute(
                "select ratings_digest from duel.sync_runs where status = 'ok' and ratings_digest is not null"
                " order by finished_at desc limit 1"
            ).fetchone()
        return None if row is None else str(row["ratings_digest"])

    def finish_run(
        self, run_id: str, status: RunStatus, counts: RunCounts, ratings: list[RatingRow], digest: str | None
    ) -> None:
        with db_errors(), self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                "update duel.sync_runs set status = %s, finished_at = now(), inserted = %s, skipped = %s,"
                " rejected = %s, ratings_digest = %s where id = %s",
                (status, counts.inserted, counts.skipped, counts.rejected, digest if ratings else None, run_id),
            )
            if ratings:
                cols = _columns(ratings[0])
                cur.executemany(
                    _insert("rating_snapshots", ["sync_run_id", *cols], None),
                    [[run_id, *(getattr(r, c) for c in cols)] for r in ratings],
                )

    @contextmanager
    def rebuilding(self) -> Iterator[None]:
        """Truncate the derived tables and reload in one transaction (owner only); readers see old or new data."""
        with db_errors(), self._conn.transaction():
            names = sql.SQL(", ").join(sql.Identifier("duel", t) for t in _DERIVED)
            self._conn.execute(sql.SQL("truncate {}").format(names))
            yield


class PublicReader:
    """Reads the ``public_v`` views in a read-only transaction, as a role that can't write or own anything."""

    def __init__(self, conn: Conn) -> None:
        f = role_facts(conn)
        if f.superuser or f.createrole or f.bypassrls or f.writer or f.ingest or f.owner or not f.reader:
            raise ConfigError("the export role must be a duel_reader member with no write, owner or admin rights")
        refuse_definer_functions(conn)
        self._conn = conn

    def export(self) -> dict[str, list[dict[str, object]]]:
        out: dict[str, list[dict[str, object]]] = {}
        with self._conn.transaction():
            self._conn.execute("set transaction read only")
            for view in PUBLIC_VIEWS:
                cur = self._conn.execute(sql.SQL("select * from public_v.{}").format(sql.Identifier(view)))
                out[view] = [dict(r) for r in cur]
        return out
