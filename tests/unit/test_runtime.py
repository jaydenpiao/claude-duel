"""Optional docker runtime (gVisor runsc, ADR 0011): argv, config and clean fallback."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox, docker_runtimes
from claude_duel.app.container import docker_runtime
from claude_duel.config import Settings, load_settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import SandboxLimits


def _fake_docker(tmp_path: Path, out: str, code: int = 0) -> tuple[str, ...]:
    script = tmp_path / "docker"
    script.write_text(f"#!/bin/sh\necho '{out}'\nexit {code}\n")
    script.chmod(0o755)
    return (str(script),)


def test_argv_adds_runtime_only_when_set() -> None:
    assert "--runtime" not in DockerSandbox().argv("n", SandboxLimits())
    argv = DockerSandbox(runtime="runsc").argv("n", SandboxLimits())
    assert argv[argv.index("--runtime") + 1] == "runsc"
    assert argv.index("--runtime") < argv.index("python:3.12-slim")


def test_docker_runtimes_parses_and_fails_empty(tmp_path: Path) -> None:
    assert docker_runtimes(_fake_docker(tmp_path, "runc runsc ")) == {"runc", "runsc"}
    assert docker_runtimes(_fake_docker(tmp_path, "runc", code=1)) == frozenset()
    assert docker_runtimes(("/nonexistent/docker",)) == frozenset()


def test_fallback_when_not_registered(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    docker = _fake_docker(tmp_path, "runc")
    assert docker_runtime(Settings(docker_command=docker)) is None
    with caplog.at_level(logging.WARNING, logger="claude_duel"):
        assert docker_runtime(Settings(docker_command=docker, docker_runtime="runsc")) is None
    assert "not registered" in caplog.text
    ok = Settings(docker_command=_fake_docker(tmp_path, "runc runsc"), docker_runtime="runsc")
    assert docker_runtime(ok) == "runsc"


def test_env_and_validation() -> None:
    assert load_settings(env={"DUEL_DOCKER_RUNTIME": "runsc"}).docker_runtime == "runsc"
    with pytest.raises(ConfigError):
        load_settings(env={"DUEL_DOCKER_RUNTIME": "runsc --privileged"})
