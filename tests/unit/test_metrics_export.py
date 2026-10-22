"""JSON-lines metrics sink and tee (#11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_duel.adapters.metrics.jsonl_metrics import JsonlMetrics, TeeMetrics
from claude_duel.app.container import build_metrics
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.observability import InMemoryMetrics, trace


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_jsonl_writes_one_line_per_metric(tmp_path: Path) -> None:
    path = tmp_path / "m" / "metrics.jsonl"
    sink = JsonlMetrics(path, "m1")
    with trace("t1"):
        sink.incr("a", 2, z="1", b="2")
        sink.timing("b", 0.1234567)
    first, second = _lines(path)
    assert first | {"ts": 0} == {
        "ts": 0, "match_id": "m1", "trace_id": "t1", "kind": "counter", "name": "a", "value": 2,
        "tags": {"b": "2", "z": "1"},
    }  # fmt: skip
    assert (second["kind"], second["value"]) == ("timing", 0.123457)


def test_jsonl_scrubs_flags(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    JsonlMetrics(path, "m").incr("x", why="DUEL{" + "a" * 32 + "}")
    assert "DUEL{" + "a" * 32 not in path.read_text()


def test_tee_and_build(tmp_path: Path) -> None:
    mem = InMemoryMetrics()
    TeeMetrics((mem, InMemoryMetrics())).timing("t", 1.0)
    assert mem.timings["t"] == [1.0]
    assert build_metrics(Settings(store_dir=tmp_path), "m", mem) is mem
    tee = build_metrics(Settings(store_dir=tmp_path, metrics="jsonl"), "m", mem)
    tee.incr("y")
    assert mem.counters["y"] == 1
    assert _lines(tmp_path / "metrics.jsonl")[0]["name"] == "y"


def test_cli_run_exports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUEL_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    monkeypatch.setenv("DUEL_ROUNDS", "1")
    monkeypatch.setenv("DUEL_METRICS", "jsonl")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["run", "--match-id", "mx"]) == 0
    rows = _lines(tmp_path / "metrics.jsonl")
    assert rows
    assert {r["match_id"] for r in rows} == {"mx"}
    assert "provider.tokens" in {r["name"] for r in rows}
