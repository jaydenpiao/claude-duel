"""Large tables are split so no statement exceeds the Postgres bind-parameter limit."""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.adapters.store.results_sql import MAX_PARAMS, insert_rows


@dataclass(frozen=True)
class Row:
    a: int
    b: str
    c: int
    d: int
    e: int
    f: int
    g: int


def test_rows_are_chunked_under_limit() -> None:
    rows = [Row(i, "x", 0, 0, 0, 0, 0) for i in range(20_000)]
    stmts = insert_rows("events", None, rows)
    assert len(stmts) == 3
    assert all(len(params) <= MAX_PARAMS for _, params in stmts)
    assert sum(len(params) for _, params in stmts) == 7 * len(rows)
    assert [p[0] for _, p in stmts] == [0, MAX_PARAMS // 7, 2 * (MAX_PARAMS // 7)]


def test_prefix_counts_toward_limit() -> None:
    rows = [Row(i, "x", 0, 0, 0, 0, 0) for i in range(MAX_PARAMS // 8 + 1)]
    stmts = insert_rows("rating_snapshots", None, rows, prefix=("run",))
    assert len(stmts) == 2
    assert all(len(params) <= MAX_PARAMS for _, params in stmts)


def test_small_table_is_one_statement() -> None:
    assert len(insert_rows("events", None, [Row(1, "x", 0, 0, 0, 0, 0)])) == 1
