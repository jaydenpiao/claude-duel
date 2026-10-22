"""End-to-end through the CLI entry point with the fake provider."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox
from claude_duel.app.container import make_provider, make_sandbox
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain.models import AgentSpec
from claude_duel.observability import NullMetrics


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DUEL_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    monkeypatch.setenv("DUEL_ROUNDS", "2")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return tmp_path


def test_run_resume_replay(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--match-id", "e2e"]) == 0
    assert "match e2e: complete after round 2" in capsys.readouterr().out
    assert main(["resume", "e2e"]) == 0  # resuming a complete match is a no-op
    assert main(["replay", "e2e", "--transcript"]) == 0
    out = capsys.readouterr().out
    assert "replay consistent=True" in out
    assert "round_scored" in out


def test_replay_detects_tampering(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "--match-id", "tamper"])
    path = env / "store" / "tamper.json"
    data = json.loads(path.read_text())
    first = next(iter(data["attacks"]))
    data["attacks"][first]["solved"] = not data["attacks"][first]["solved"]
    path.write_text(json.dumps(data))
    assert main(["replay", "tamper"]) == 1


def test_errors(env: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["resume", "missing"]) == 2
    assert main(["replay", "missing"]) == 2
    cfg = tmp_path / "openai.toml"
    cfg.write_text('[[agents]]\nname = "a"\nprovider = "openai"\n[[agents]]\nname = "b"\n')
    assert main(["--config", str(cfg), "run"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    cfg.write_text('openai_model = "gpt-4o"\n')
    assert main(["--config", str(cfg), "run"]) == 2
    cfg.write_text('[[agents]]\nname = "a"\nprovider = "nope"\n[[agents]]\nname = "b"\n')
    assert main(["--config", str(cfg), "run"]) == 2


def test_budget_abort_then_resume(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DUEL_MATCH_MAX_CALLS", "5")
    assert main(["run", "--match-id", "cap"]) == 2
    assert "budget exceeded" in capsys.readouterr().err
    monkeypatch.setenv("DUEL_MATCH_MAX_CALLS", "500")
    assert main(["resume", "cap"]) == 0


def test_container_wiring(env: Path) -> None:

    s = Settings(openai_api_key=SecretStr("sk-x"))
    assert isinstance(make_provider(AgentSpec(name="a", provider="openai"), s, NullMetrics()), RetryingProvider)
    assert isinstance(make_sandbox("docker"), DockerSandbox)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_live_openai_tiny_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One tiny real match; capped at $0.05. Run explicitly: pytest -m live."""
    monkeypatch.setenv("DUEL_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    cfg = tmp_path / "live.toml"
    cfg.write_text(
        'openai_model = "gpt-4.1-nano"\n'
        '[[agents]]\nname = "alice"\nprovider = "openai"\n[[agents]]\nname = "bob"\nprovider = "openai"\n'
        "[rules]\nrounds = 1\nchallenges_per_round = 1\nmax_attempts = 2\nattack_token_budget = 6000\n"
        "[budget]\nmatch_max_usd = 0.05\nrun_max_usd = 0.05\nmatch_max_calls = 12\n"
    )
    code = main(["--config", str(cfg), "run", "--match-id", "live"])
    out = capsys.readouterr().out
    print(out)
    assert code == 0
    assert "complete" in out
