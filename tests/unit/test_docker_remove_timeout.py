"""#307 N3 regression: a docker daemon that hangs on `rm` cannot block a run forever; removal gives up after
``_PROBE_SECONDS`` and raises, so the caller's reap path is skipped instead of waiting."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from claude_duel.adapters.sandbox import docker_sandbox
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox


def test_a_hung_rm_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hung = tmp_path / "docker"
    hung.write_text("#!/bin/sh\nexec sleep 30\n")
    hung.chmod(0o755)
    monkeypatch.setattr(docker_sandbox, "_PROBE_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        DockerSandbox((str(hung),))._remove("duel-test-hung")
    assert time.monotonic() - started < 5
