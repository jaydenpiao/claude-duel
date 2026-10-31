"""Large tables are split so no statement exceeds the Postgres bind-parameter limit."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.results_sql import MAX_PARAMS, SqlResults, insert_rows
from claude_duel.app.engine import MatchEngine
from claude_duel.app.sync import rows
from claude_duel.domain.models import MatchState


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


class _Ex:
    """Records each batch; every statement reports one row per value tuple it inserts."""

    def __init__(self) -> None:
        self.stmts: list[tuple[str, Sequence[object]]] = []

    def batch(self, stmts: Sequence[tuple[str, Sequence[object]]]) -> list[int]:
        self.stmts = list(stmts)
        return [sql.count("(%s") for sql, _ in stmts]


def test_ingest_chunks_a_long_match_and_sums_event_counts(
    make_engine: Callable[..., MatchEngine],
    new_state: Callable[..., MatchState],
    event_log: FileEventLog,
    store: FileMatchStore,
) -> None:
    make_engine().run(new_state("m1"))
    p = rows(store.snapshot("m1"), event_log.chain("m1"))
    e = p.events[0]
    many = tuple(replace(e, event_hash=f"h{i}", seq=i) for i in range(MAX_PARAMS // 7 + 2))  # > 9,363 events
    ex = _Ex()
    n = SqlResults(ex).ingest(replace(p, events=many))
    events = [(sql, params) for sql, params in ex.stmts if 'duel."events"' in sql]
    assert len(events) == 2
    assert all(len(params) <= MAX_PARAMS for _, params in ex.stmts)
    assert n == len(many)


def test_no_rows_no_statements() -> None:
    assert insert_rows("events", None, []) == []
