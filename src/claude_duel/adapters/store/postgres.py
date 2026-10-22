"""Postgres transport for the results index (ADR 0012): psycopg connection, ``PgExecutor`` and the public export.

The SQL itself lives in ``results_sql`` (shared with ``neon_http``). Connection URLs come only from the environment
and are never logged or put in an error message. ``migrate`` and ``--rebuild`` need the schema owner
(``MIGRATE_DATABASE_URL``); routine ``duel sync`` should run as a ``duel_ingest`` login (``DATABASE_URL``), which
cannot delete or truncate. ``PublicReader`` refuses any role that can write.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from claude_duel.adapters.store import results_sql
from claude_duel.adapters.store.postgres_url import require_tls
from claude_duel.adapters.store.results_sql import Row, SqlResults, Stmt
from claude_duel.domain.errors import ConfigError, ResultsDbError

__all__ = ["require_tls"]

PUBLIC_VIEWS = ("matches", "match_agents", "leaderboard", "rating_history", "model_daily", "cost_daily")
Conn = psycopg.Connection[dict[str, Any]]


def connect(url: str) -> Conn:
    require_tls(url)
    try:
        return psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=15)
    except psycopg.Error as exc:  # the message can name host and user; keep it out of output
        raise ConfigError(f"cannot connect to the results database ({type(exc).__name__})") from None


@contextmanager
def db_errors() -> Iterator[None]:
    """Re-raise a database error as its class and SQLSTATE only: server messages can quote row values."""
    try:
        yield
    except psycopg.Error as exc:
        raise ResultsDbError(
            f"results database error: {type(exc).__name__} ({exc.sqlstate or 'no sqlstate'})", exc.sqlstate
        ) from None


def _value(v: object) -> object:
    return Jsonb(v) if isinstance(v, dict | list) else v


class PgExecutor:
    """``results_sql.Executor`` on a psycopg connection (autocommit; ``batch``/``transaction`` nest as savepoints)."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def query(self, sql_text: str, params: Sequence[object] = ()) -> list[Row]:
        with db_errors():
            cur = self._conn.execute(sql.SQL(sql_text), [_value(v) for v in params] if params else None)
            return [dict(r) for r in cur] if cur.description else []

    def batch(self, stmts: Sequence[Stmt]) -> list[int]:
        counts = []
        with db_errors(), self._conn.transaction(), self._conn.cursor() as cur:
            for text, params in stmts:
                cur.execute(sql.SQL(text), [_value(v) for v in params] if params else None)
                counts.append(cur.rowcount)
        return counts

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with db_errors(), self._conn.transaction():
            yield


def PostgresResults(conn: Conn) -> SqlResults:  # noqa: N802 - kept as the adapter's public name
    """``ResultsStore`` on a psycopg connection."""
    return SqlResults(PgExecutor(conn))


def migrate(conn: Conn) -> list[str]:
    return results_sql.migrate(PgExecutor(conn))


def require_current(conn: Conn) -> None:
    results_sql.require_current(PgExecutor(conn))


def refuse_superuser(conn: Conn) -> None:
    results_sql.refuse_superuser(PgExecutor(conn))


def refuse_definer_functions(conn: Conn) -> None:
    results_sql.refuse_definer_functions(PgExecutor(conn))


class PublicReader:
    """Reads the ``public_v`` views in a read-only transaction, as a role that can't write or own anything."""

    def __init__(self, conn: Conn) -> None:
        row = conn.execute(
            "select r.rolsuper or r.rolcreaterole or r.rolbypassrls"
            " or pg_has_role(current_user, 'duel_writer', 'MEMBER')"
            " or pg_has_role(current_user, 'duel_ingest', 'MEMBER')"
            " or exists (select from pg_tables where schemaname = 'duel' and tableowner = current_user) as unsafe,"
            " pg_has_role(current_user, 'duel_reader', 'MEMBER') as reader"
            " from pg_roles r where r.rolname = current_user"
        ).fetchone()
        if row is None or row["unsafe"] or not row["reader"]:
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
