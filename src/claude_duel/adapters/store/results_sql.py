"""The results index in SQL, (ADR 0012), run by the ``postgres`` (psycopg) transport.

No driver imports and no I/O of its own: an ``Executor`` runs the statements. Upsert keys, migrations, role checks
and the ``ResultsStore`` logic live here once, so the memory and SQL stores behave identically (``tests/contract``).
Placeholders are ``%s``; values are plain Python (dicts and lists are JSON) and each executor adapts them.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from importlib import resources
from typing import Protocol

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

Row = dict[str, object]
Stmt = tuple[str, Sequence[object]]
LOCK_KEY = "duel.migrate"  # pg_advisory_xact_lock(hashtext(...)): one migrator at a time
UNIQUE_VIOLATION = "23505"
# Projection row type -> (table, upsert key). A key means a re-run refreshes the row in place; None means the row is
# append-only (keyed on an event hash or entrant id) and a conflict is skipped.
TABLE: dict[type, tuple[str, str | None]] = {
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
DERIVED = ("matches", "entrants", "match_agents", "match_results", "events", "calls", "attacks", "round_scores")


class Executor(Protocol):
    """Runs SQL for one connection string. Errors are ``ResultsDbError`` (class and SQLSTATE only, never values)."""

    def query(self, sql: str, params: Sequence[object] = ()) -> list[Row]:
        """One statement in its own transaction; rows as dicts."""
        ...

    def batch(self, stmts: Sequence[Stmt]) -> list[int]:
        """All statements in one transaction (all or nothing); the row count of each."""
        ...

    def transaction(self) -> AbstractContextManager[None]:
        """A transaction spanning several calls (``--rebuild``); transports that can't hold one raise ConfigError."""
        ...


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


def _applied(ex: Executor) -> dict[str, str]:
    if not ex.query("select to_regclass('duel.schema_migrations') is not null as present")[0]["present"]:
        return {}
    return {str(r["name"]): str(r["sha256"]) for r in ex.query("select name, sha256 from duel.schema_migrations")}


def migrate(ex: Executor) -> list[str]:
    """Apply pending migrations in one transaction under an advisory lock. Returns the names applied.

    The bookkeeping rows go in first: a concurrent migrator that read the same state hits the primary key after the
    lock, rolls back whole and re-reads.
    """
    pending = check_applied(_applied(ex))
    if not pending:
        return []
    stmts: list[Stmt] = [
        ("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,)),
        ("create schema if not exists duel", ()),
        (
            "create table if not exists duel.schema_migrations"
            " (name text primary key, sha256 char(64) not null, applied_at timestamptz not null default now())",
            (),
        ),
    ]
    for name, digest, text in migrations():
        if name in pending:
            stmts.append(("insert into duel.schema_migrations (name, sha256) values (%s, %s)", (name, digest)))
            stmts.append((text, ()))  # our own bundled file, never input; psycopg runs it whole (no params)
    try:
        ex.batch(stmts)
    except ResultsDbError as exc:
        if exc.sqlstate != UNIQUE_VIOLATION:
            raise
        check_applied(_applied(ex))
        return []
    return pending


def refuse_definer_functions(ex: Executor) -> None:
    """Refuse a SECURITY DEFINER function in ``duel`` or ``public_v``: it would bypass RLS and column grants."""
    rows = ex.query(
        "select count(*) as n from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname in ('duel', 'public_v') and p.prosecdef"
    )
    if rows[0]["n"]:
        raise ConfigError("a SECURITY DEFINER function exists in schema duel or public_v; remove it first")


def require_current(ex: Executor) -> None:
    """Fail clearly unless every bundled migration is applied unchanged (``duel sync`` never migrates)."""
    applied = _applied(ex)
    refuse_definer_functions(ex)
    if check_applied(applied):
        raise ConfigError("the results database schema is behind: run `duel db migrate` (as the schema owner)")


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


def role_facts(ex: Executor) -> RoleFacts:
    rows = ex.query(_ROLE_FACTS)
    if not rows:
        raise ConfigError("cannot read the current role from pg_roles")
    return RoleFacts(**{f.name: bool(rows[0][f.name]) for f in dataclasses.fields(RoleFacts)})


def refuse_superuser(ex: Executor) -> None:
    """``duel sync`` must not run as a role that ignores row-level security (superuser or BYPASSRLS)."""
    facts = role_facts(ex)
    if facts.superuser or facts.bypassrls:
        raise ConfigError("refusing to sync as a superuser or BYPASSRLS role: use a duel_ingest or owner login")


def refuse_table_owner(ex: Executor) -> None:
    """Routine ``duel sync`` must not run as a role that owns a ``duel`` table (it could truncate or drop it);
    the owner is for ``duel db migrate`` and ``--rebuild`` only."""
    if role_facts(ex).owner:
        raise ConfigError("refusing to sync as a duel table owner: use a duel_ingest login (owner only for --rebuild)")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def insert_sql(table: str, cols: Sequence[str], key: str | None, rows: int = 1) -> str:
    """A multi-row ``insert ... on conflict`` for trusted table and column names (ours, never input)."""
    one = "(" + ", ".join(["%s"] * len(cols)) + ")"
    target = f"duel.{_quote(table)} ({', '.join(map(_quote, cols))})"
    stmt = f"insert into {target} values {', '.join([one] * rows)}"  # noqa: S608 - names are ours, values are params
    if key is None:
        return stmt + " on conflict do nothing"
    sets = ", ".join(f"{_quote(c)} = excluded.{_quote(c)}" for c in cols)
    stmt += f" on conflict ({key}) do update set {sets}"
    if table == "matches":
        stmt += ", rejected = false, synced_at = now()"
    return stmt


def columns(row: object) -> list[str]:
    return [f.name for f in dataclasses.fields(row)]  # type: ignore[arg-type]


MAX_PARAMS = 65_535  # Postgres bind-parameter limit per statement


def insert_rows(table: str, key: str | None, rows: Sequence[object], prefix: Sequence[object] = ()) -> list[Stmt]:
    """Insert statements for every row, chunked under ``MAX_PARAMS`` (``prefix`` values go first, e.g. a run id)."""
    if not rows:
        return []
    cols = columns(rows[0])
    names = [*(["sync_run_id"] if prefix else []), *cols]
    per = MAX_PARAMS // len(names)
    stmts: list[Stmt] = []
    for at in range(0, len(rows), per):
        chunk = rows[at : at + per]
        params = [v for r in chunk for v in (*prefix, *(getattr(r, c) for c in cols))]
        stmts.append((insert_sql(table, names, key, len(chunk)), params))
    return stmts


class SqlResults:
    """``ResultsStore`` over any ``Executor``: ``ingest`` is one atomic batch, idempotent by hash and upsert key."""

    def __init__(self, ex: Executor) -> None:
        self._ex = ex

    def stored(self, match_id: str) -> Stored:
        row = self._ex.query("select projection_version, rejected from duel.matches where match_id = %s", (match_id,))
        events = self._ex.query("select event_hash from duel.events where match_id = %s", (match_id,))
        hashes = frozenset(str(r["event_hash"]) for r in events)
        if not row:
            return Stored(hashes)
        version = row[0]["projection_version"]
        return Stored(hashes, None if version is None else int(str(version)), bool(row[0]["rejected"]))

    def ingest(self, projection: MatchProjection) -> int:
        p = projection
        groups: list[Sequence[object]] = [[p.match], p.entrants, p.agents, [p.result], p.events, p.calls]
        groups += [p.attacks, p.scores]
        stmts: list[Stmt] = [
            ("insert into duel.match_first_seen (match_id) values (%s) on conflict do nothing", (p.match.match_id,))
        ]
        events = range(0)
        for rows in groups:
            if rows:
                table, key = TABLE[type(rows[0])]
                chunk = insert_rows(table, key, rows)
                if table == "events":
                    events = range(len(stmts), len(stmts) + len(chunk))
                stmts += chunk
        counts = self._ex.batch(stmts)
        return sum(counts[i] for i in events)

    def start_run(self, run_id: str, git_sha: str | None) -> None:
        self._ex.batch([("insert into duel.sync_runs (id, git_sha) values (%s, %s)", (run_id, git_sha))])

    def reject(self, run_id: str, match_id: str, reason: str) -> None:
        self._ex.batch(
            [
                (
                    "insert into duel.sync_rejects (sync_run_id, match_id, reason) values (%s, %s, %s)"
                    " on conflict do nothing",
                    (run_id, match_id, reason),
                ),
                ("update duel.matches set rejected = true where match_id = %s", (match_id,)),
            ]
        )

    def last_ratings_digest(self) -> str | None:
        rows = self._ex.query(
            "select ratings_digest from duel.sync_runs where status = 'ok' and ratings_digest is not null"
            " order by finished_at desc limit 1"
        )
        return str(rows[0]["ratings_digest"]) if rows else None

    def finish_run(
        self, run_id: str, status: RunStatus, counts: RunCounts, ratings: list[RatingRow], digest: str | None
    ) -> None:
        stmts: list[Stmt] = [
            (
                "update duel.sync_runs set status = %s, finished_at = now(), inserted = %s, skipped = %s,"
                " rejected = %s, ratings_digest = %s where id = %s",
                (status, counts.inserted, counts.skipped, counts.rejected, digest if ratings else None, run_id),
            )
        ]
        if ratings:
            stmts += insert_rows("rating_snapshots", None, ratings, prefix=(run_id,))
        self._ex.batch(stmts)

    @contextmanager
    def rebuilding(self) -> Iterator[None]:
        """Truncate the derived tables and reload in one transaction (owner only); readers see old or new data."""
        with self._ex.transaction():
            self._ex.query("truncate " + ", ".join(f"duel.{_quote(t)}" for t in DERIVED))
            yield
