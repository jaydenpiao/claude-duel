"""docker_probe refuses a daemon that can't enforce cgroup limits (rootless without systemd ignores --memory etc.)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.adapters.sandbox.docker_sandbox import docker_probe


def _fake_docker(tmp_path: Path, out: str) -> tuple[str, ...]:
    script = tmp_path / "docker"
    script.write_text(f"#!/bin/sh\necho '{out}'\n")
    script.chmod(0o755)
    return (str(script),)


@pytest.mark.parametrize(
    ("out", "problem"),
    [
        ('26.1|systemd|["name=seccomp,profile=builtin"]', None),
        ('26.1|cgroupfs|["name=seccomp,profile=builtin"]', None),
        ('26.1|none|["name=seccomp,profile=builtin","name=rootless"]', "cgroup driver none"),
        ('26.1|systemd|["name=apparmor"]', "seccomp"),
        ("|", "unreachable"),
    ],
)
def test_probe(tmp_path: Path, out: str, problem: str | None) -> None:
    got = docker_probe(_fake_docker(tmp_path, out))
    assert got is None if problem is None else problem in (got or "")
