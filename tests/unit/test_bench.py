"""Benchmark slice harness with fake agents (#45)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_duel.adapters.providers.fake import TEMPLATES, FakeProvider, is_template
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.app.bench import BenchTask, load_tasks, run_bench, solve, solve_rate
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec, SandboxLimits


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "tasks.jsonl"
    rows = [
        {"id": f"t-{t.key}", "title": t.title, "source": t.source, "origin": "local", "license": "MIT"}
        for t in TEMPLATES
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n")
    return path


def test_load_and_reject(tmp_path: Path) -> None:
    assert len(load_tasks(_manifest(tmp_path))) == len(TEMPLATES)
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "x"}\n')
    with pytest.raises(ConfigError, match="bad bench manifest"):
        load_tasks(bad)
    dup = tmp_path / "dup.jsonl"
    line = json.dumps({"id": "a", "title": "t", "source": "print(1)", "origin": "local", "license": "MIT"})
    dup.write_text(f"{line}\n{line}\n")
    with pytest.raises(ConfigError, match="duplicate"):
        load_tasks(dup)
    with pytest.raises(ConfigError):
        load_tasks(tmp_path / "missing.jsonl")


def test_solve_counts_tries_and_malformed() -> None:
    t = TEMPLATES[0]
    task = BenchTask(id="m", title=t.title, source=t.source, origin="local", license="MIT")
    sandbox, lim = SubprocessSandbox(is_template), SandboxLimits()
    weak = solve(task, AgentSpec(name="a", skill=0), FakeProvider(skill=0), sandbox, lim, 2)
    assert (weak.solved, weak.attempts) == (False, 2)
    agent, n = AgentSpec(name="b", skill=len(TEMPLATES)), len(TEMPLATES) + 1
    runs = [
        solve(BenchTask(id="m", title=x.title, source=x.source, origin="local", license="MIT"), agent,
              FakeProvider(skill=n, malformed_first=1), sandbox, lim, n)
        for x in TEMPLATES
    ]  # fmt: skip
    solved = [r for r in runs if r.solved]
    assert len(solved) == len(TEMPLATES) - 1  # the malformed first answer used up one known payload's try
    assert min(r.attempts for r in solved) == 2


def test_run_bench_skill_orders_solve_rate(tmp_path: Path) -> None:
    settings = Settings(
        sandbox="subprocess", agents=(AgentSpec(name="weak", skill=1), AgentSpec(name="strong", skill=len(TEMPLATES)))
    )
    rates = solve_rate(run_bench(settings, _manifest(tmp_path), len(TEMPLATES)))
    assert rates["strong"] == 1.0
    assert rates["weak"] < rates["strong"]


def test_refuses_live_agents(tmp_path: Path) -> None:
    live = Settings(agents=(AgentSpec(name="a", provider="openai"), AgentSpec(name="b")))
    with pytest.raises(ConfigError, match="fake agents only"):
        run_bench(live, _manifest(tmp_path), 1)


def test_cli_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manifest = str(_manifest(tmp_path))
    assert main(["bench", manifest, "--tries", "2"]) == 0
    assert "solve rate:" in capsys.readouterr().out
    assert main(["bench", manifest, "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["solve_rate"]) == {"alice", "bob"}
    assert main(["bench", manifest, "--tries", "0"]) == 2
