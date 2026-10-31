"""Routine ``duel sync`` refuses a role that owns a duel table (the owner is for migrate and --rebuild only)."""

from __future__ import annotations

from typing import Any

import pytest

from claude_duel.adapters.store.results_sql import refuse_table_owner
from claude_duel.domain.errors import ConfigError


class _Conn:
    def __init__(self, owner: bool) -> None:
        self.owner = owner
        self.sql = ""

    def query(self, sql: str, *_: Any) -> list[dict[str, bool]]:
        self.sql = sql
        return [{"owner": self.owner}]


def test_table_owner_is_refused() -> None:
    conn = _Conn(owner=True)
    with pytest.raises(ConfigError, match="table owner"):
        refuse_table_owner(conn)  # type: ignore[arg-type]
    assert "tableowner = current_user" in conn.sql


def test_ingest_login_passes() -> None:
    refuse_table_owner(_Conn(owner=False))  # type: ignore[arg-type]
