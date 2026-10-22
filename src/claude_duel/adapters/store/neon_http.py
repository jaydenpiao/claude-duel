"""Neon serverless SQL over HTTPS for the results index (``DUEL_RESULTS_BACKEND=neon-http``, ADR 0012).

For hosts that can reach 443 but not 5432. Each ``query`` is one POST to Neon's ``/sql`` endpoint and each
``batch`` one POST of ``{"queries": [...]}``, which Neon runs as a single transaction. The SQL is
``results_sql``'s, so behaviour matches the ``postgres`` transport (``tests/contract``).

The connection string is a full credential sent in the ``Neon-Connection-String`` header over TLS. It comes only
from the environment and is never logged or put in an error: errors carry the class and SQLSTATE only.
A transaction can't span requests, so ``--rebuild`` needs the ``postgres`` transport.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from claude_duel.adapters.store.results_sql import Row, Stmt
from claude_duel.domain.errors import ConfigError, ResultsDbError

_FIRST_LABEL = re.compile(r"^[^.]+\.")
_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})
_PLACEHOLDER = re.compile(r"%s")
# Postgres type OIDs -> parser for Neon's text output; anything else stays text.
_PARSE: dict[int, Callable[[str], object]] = {
    16: lambda v: v == "t",
    20: int,
    21: int,
    23: int,
    26: int,
    700: float,
    701: float,
    1700: Decimal,
    114: json.loads,
    3802: json.loads,
}


def endpoint_for(url: str) -> str:
    """Neon's SQL endpoint for a connection string: the first host label becomes ``api`` (as the serverless driver)."""
    host = urlsplit(url).hostname or ""
    if "." not in host:
        raise ConfigError("neon-http needs a Neon host in DATABASE_URL (or set DUEL_NEON_HTTP_ENDPOINT)")
    return f"https://{_FIRST_LABEL.sub('api.', host)}/sql"


def check_endpoint(endpoint: str) -> str:
    """Plain http is allowed only to a local proxy: the request carries the connection string."""
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    local = host in _LOCAL or host.endswith(".localtest.me")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ConfigError("DUEL_NEON_HTTP_ENDPOINT must be https (plain http only for a local proxy)")
    return endpoint


def _param(v: object) -> object:
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict | list | tuple):
        return json.dumps(v)
    return str(v)


def _query(text: str, params: Sequence[object]) -> dict[str, object]:
    if params:
        n = iter(range(1, len(params) + 1))
        text = _PLACEHOLDER.sub(lambda _: f"${next(n)}", text)
    return {"query": text, "params": [_param(v) for v in params]}


def _rows(result: dict[str, Any]) -> list[Row]:
    fields = result.get("fields") or []
    parsers = [_PARSE.get(int(f["dataTypeID"]), str) for f in fields]
    return [
        {f["name"]: None if v is None else parse(v) for f, parse, v in zip(fields, parsers, row, strict=True)}
        for row in result.get("rows") or []
    ]


class NeonHttpExecutor:
    """``results_sql.Executor`` over Neon's SQL-over-HTTPS endpoint."""

    def __init__(self, url: str, endpoint: str | None = None, client: httpx.Client | None = None) -> None:
        self._endpoint = check_endpoint(endpoint or endpoint_for(url))
        self._headers = {
            "Neon-Connection-String": url,
            "Neon-Raw-Text-Output": "true",
            "Neon-Array-Mode": "true",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.Client(timeout=30.0)

    def _post(self, body: dict[str, object], headers: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            resp = self._client.post(self._endpoint, json=body, headers={**self._headers, **(headers or {})})
        except httpx.HTTPError as exc:  # the request (and its headers) never reach the message
            raise ResultsDbError(f"results database unreachable ({type(exc).__name__})") from None
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != httpx.codes.OK:
            code = data.get("code") if isinstance(data.get("code"), str) and data.get("code") else None
            raise ResultsDbError(f"results database error: HTTP {resp.status_code} ({code or 'no sqlstate'})", code)
        return data

    def query(self, sql: str, params: Sequence[object] = ()) -> list[Row]:
        return _rows(self._post(_query(sql, params)))

    def batch(self, stmts: Sequence[Stmt]) -> list[int]:
        data = self._post(
            {"queries": [_query(text, params) for text, params in stmts]},
            {"Neon-Batch-Isolation-Level": "ReadCommitted", "Neon-Batch-Read-Only": "false"},
        )
        return [int(r.get("rowCount") or 0) for r in data.get("results") or []]

    def transaction(self) -> AbstractContextManager[None]:
        raise ConfigError("`duel sync --rebuild` needs DUEL_RESULTS_BACKEND=postgres (neon-http can't span requests)")
