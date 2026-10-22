"""Postgres results index (ADR 0012): migrations, idempotent ingest and the read-only public export.

Connection URLs come only from the environment and are never logged or put in an error message.
``PostgresResults`` writes as the migrating owner; ``PublicReader`` refuses any role that can write.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from claude_duel.domain.errors import ConfigError
from claude_duel.ports import Row

PUBLIC_VIEWS = ("matches", "match_agents", "leaderboard", "rating_history", "model_daily", "cost_daily")
# Rows a re-run may refresh in place; every other table is append-only and keyed on an event hash.
_UPSERT = {"matches": "match_id", "match_results": "match_id", "match_agents": "match_id, agent_name"}
_TABLES = (
    "matches",
    "entrants",
    "match_agents",
    "match_results",
    "events",
    "calls",
    "attacks",
    "round_scores",
    "rating_snapshots",
    "sync_rejects",
    "sync_runs",
)


def connect(url: str) -> psycopg.Connection[dict[str, Any]]:
    try:
        return psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=15)
    except psycopg.Error as exc:  # the message can name host and user; keep it out of output
        raise ConfigError(f"cannot connect to the results database ({type(exc).__name__})") from None


def migrate(conn: psycopg.Connection[dict[str, Any]]) -> list[str]:
    """Apply pending ``migrations/NNNN_*.sql`` in order, each in its own transaction. Returns the names applied."""
    with conn.transaction():
        conn.execute("create table if not exists public.duel_migrations (name text primary key)")
        done = {r["name"] for r in conn.execute("select name from public.duel_migrations")}
    applied = []
    for res in sorted(resources.files(__package__).joinpath("migrations").iterdir(), key=lambda r: r.name):
        if res.name.endswith(".sql") and res.name not in done:
            with conn.transaction():
                conn.execute(sql.SQL(res.read_text(encoding="utf-8")))
                conn.execute("insert into public.duel_migrations values (%s)", (res.name,))
            applied.append(res.name)
    return applied


def _value(v: object) -> object:
    return Jsonb(v) if isinstance(v, dict | list) else v


def _insert(table: str, row: Row) -> tuple[sql.Composed, list[object]]:
    cols = list(row)
    stmt = sql.SQL("insert into duel.{} ({}) values ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    key = _UPSERT.get(table)
    if key is None:
        stmt += sql.SQL(" on conflict do nothing")
    else:
        sets = sql.SQL(", ").join(sql.SQL("{0} = excluded.{0}").format(sql.Identifier(c)) for c in cols)
        stmt += sql.SQL(" on conflict ({}) do update set ").format(sql.SQL(key)) + sets
        if table == "matches":
            stmt += sql.SQL(", synced_at = now()")
    return stmt, [_value(row[c]) for c in cols]


class PostgresResults:
    """``ResultsStore`` on Postgres. Each match is one transaction."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self._conn = conn

    def stored_hashes(self, match_id: str) -> set[str]:
        cur = self._conn.execute("select event_hash from duel.events where match_id = %s", (match_id,))
        return {r["event_hash"] for r in cur}

    def ingest(self, tables: dict[str, list[Row]]) -> int:
        with self._conn.transaction():
            for table, rows in tables.items():
                for row in rows:
                    self._conn.execute(*_insert(table, row))
        return len(tables.get("events", []))

    def start_run(self) -> int:
        with self._conn.transaction():
            row = self._conn.execute("insert into duel.sync_runs default values returning id").fetchone()
        assert row is not None
        return int(row["id"])

    def reject(self, run_id: int, match_id: str, reason: str) -> None:
        with self._conn.transaction():
            self._conn.execute(
                *_insert("sync_rejects", {"sync_run_id": run_id, "match_id": match_id, "reason": reason})
            )

    def finish_run(self, run_id: int, counts: dict[str, int], ratings: list[Row]) -> None:
        with self._conn.transaction():
            self._conn.execute(
                "update duel.sync_runs set inserted = %(inserted)s, skipped = %(skipped)s, rejected = %(rejected)s"
                " where id = %(id)s",
                counts | {"id": run_id},
            )
            for row in ratings:
                self._conn.execute(*_insert("rating_snapshots", {"sync_run_id": run_id} | row))

    def clear(self) -> None:
        """``--rebuild``: empty every table (schema and roles stay)."""
        with self._conn.transaction():
            names = sql.SQL(", ").join(sql.Identifier("duel", t) for t in _TABLES)
            self._conn.execute(sql.SQL("truncate {} restart identity cascade").format(names))


class PublicReader:
    """Reads the ``public_v`` views in a read-only transaction, as a role that can't write or own anything."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        row = conn.execute(
            "select r.rolsuper or r.rolcreaterole or r.rolbypassrls"
            " or pg_has_role(current_user, 'duel_writer', 'MEMBER')"
            " or exists (select from pg_tables where schemaname = 'duel' and tableowner = current_user) as unsafe,"
            " pg_has_role(current_user, 'duel_reader', 'MEMBER') as reader"
            " from pg_roles r where r.rolname = current_user"
        ).fetchone()
        if row is None or row["unsafe"] or not row["reader"]:
            raise ConfigError("the export role must be a duel_reader member with no write, owner or admin rights")
        self._conn = conn

    def export(self) -> dict[str, list[Row]]:
        out: dict[str, list[Row]] = {}
        with self._conn.transaction():
            self._conn.execute("set transaction read only")
            for view in PUBLIC_VIEWS:
                cur = self._conn.execute(sql.SQL("select * from public_v.{}").format(sql.Identifier(view)))
                out[view] = [dict(r) for r in cur]
        return out
