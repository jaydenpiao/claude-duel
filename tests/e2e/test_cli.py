"""End-to-end through the CLI entry point with the fake provider."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.app.container import make_provider, make_sandbox
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
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
    out = capsys.readouterr().out
    assert "fake-1 (A) vs fake-1 (B)" in out
    assert "winner: " in out
    assert out.rstrip().endswith("replay verified ✓")
    assert "of $33 cap" in out  # #195: the spend line shows the enforced cap, never the key limit
    assert "$50" not in out
    assert main(["resume", "e2e", "--format", "json"]) == 0  # resuming a complete match is a no-op
    summary = json.loads(capsys.readouterr().out)
    assert (summary["match_id"], summary["stage"], summary["round"]) == ("e2e", "complete", 2)
    assert summary["est_usd"] == 0
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
    monkeypatch.setenv("DUEL_RUN_MAX_CALLS", "5")  # run caps stay a resumable failure (ADR 0014)
    assert main(["run", "--match-id", "cap"]) == 2
    assert "budget exceeded" in capsys.readouterr().err
    monkeypatch.setenv("DUEL_RUN_MAX_CALLS", "500")
    assert main(["resume", "cap"]) == 0


def test_match_cap_is_terminal_and_resume_is_a_no_op(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DUEL_MATCH_MAX_CALLS", "5")
    assert main(["run", "--match-id", "cap"]) == 3
    assert "match capped: match_calls 5/5 by " in capsys.readouterr().err
    logs = sorted(env.rglob("cap.events.jsonl"))
    before = [p.read_bytes() for p in logs]
    monkeypatch.setenv("DUEL_MATCH_MAX_CALLS", "500")  # raising the cap does not reopen a capped match
    assert main(["resume", "cap"]) == 3
    assert [p.read_bytes() for p in logs] == before
    assert logs


OPENAI = (AgentSpec(name="a", provider="openai"), AgentSpec(name="b"))
FAKE = (AgentSpec(name="a"), AgentSpec(name="b"))


def test_container_wiring(env: Path) -> None:
    s = Settings(api_keys={"openai": SecretStr("sk-x")})
    assert isinstance(make_provider(OPENAI[0], s, NullMetrics()), RetryingProvider)


def test_real_models_never_use_subprocess(env: Path) -> None:
    with pytest.raises(ConfigError, match="require the docker sandbox"):
        make_sandbox(Settings(sandbox="subprocess"), OPENAI)
    assert isinstance(make_sandbox(Settings(sandbox="subprocess"), FAKE), SubprocessSandbox)


@pytest.mark.parametrize("kind", ["auto", "docker"])
def test_docker_unavailable_fails_closed(env: Path, kind: str) -> None:
    broken = Settings(sandbox=kind, docker_command=("/nonexistent/docker",))
    with pytest.raises(ConfigError, match="docker sandbox required but unavailable"):
        make_sandbox(broken, OPENAI)


def test_auto_falls_back_only_for_fake(env: Path) -> None:
    broken = Settings(sandbox="auto", docker_command=("/nonexistent/docker",))
    assert isinstance(make_sandbox(broken, FAKE), SubprocessSandbox)
    with pytest.raises(ConfigError):
        make_sandbox(broken.model_copy(update={"sandbox": "docker"}), FAKE)


def test_cli_refuses_openai_without_docker(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DUEL_SANDBOX", "auto")
    monkeypatch.setenv("DUEL_DOCKER", "/nonexistent/docker")
    cfg = tmp_path / "o.toml"
    cfg.write_text('[[agents]]\nname = "a"\nprovider = "openai"\n[[agents]]\nname = "b"\n')
    assert main(["--config", str(cfg), "run"]) == 2


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_live_openai_tiny_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One tiny real match; capped at $0.05. Run explicitly: pytest -m live."""
    monkeypatch.setenv("DUEL_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("DUEL_SANDBOX", "docker")  # real models only run in Docker
    cfg = tmp_path / "live.toml"
    cfg.write_text(
        'openai_model = "gpt-4o-mini"\n'
        '[[agents]]\nname = "alice"\nprovider = "openai"\n[[agents]]\nname = "bob"\nprovider = "openai"\n'
        "[rules]\nrounds = 1\nchallenges_per_round = 1\nmax_attempts = 2\nattack_token_budget = 6000\n"
        "[budget]\nmatch_max_usd = 0.05\nrun_max_usd = 0.05\nmatch_max_calls = 12\n"
    )
    code = main(["--config", str(cfg), "run", "--match-id", "live"])
    out = capsys.readouterr().out
    print(out)
    assert code == 0
    assert "replay verified ✓" in out
    assert "of $33 cap" in out  # the enforced lifetime cap, not the key limit ($50)
    assert "$50" not in out
