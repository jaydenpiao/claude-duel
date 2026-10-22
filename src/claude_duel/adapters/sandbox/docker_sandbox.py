"""Docker sandbox: the security boundary for real-model matches (ADR 0003)."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Sequence

from claude_duel.adapters.sandbox.common import execute
from claude_duel.adapters.sandbox.runner import PYTHON_FLAGS, frame
from claude_duel.domain.models import SandboxLimits
from claude_duel.ports import RunResult

DEFAULT_IMAGE = "python:3.12-slim"
# Host-side deadline on top of the in-container wall limit, to absorb container start-up.
STARTUP_GRACE_SECONDS = 5.0
TIMEOUT_EXIT = 124  # coreutils `timeout` exit status when the wall limit fires
SIGNAL_EXIT_BASE = 128  # container exit status 128+N means killed by signal N
_PROBE_SECONDS = 10.0
_SELF_TEST_FLAG = "DUEL{" + "0" * 32 + "}"


def docker_probe(docker: Sequence[str]) -> str | None:
    """Return why the Docker sandbox can't be used, or None if the daemon is reachable with seccomp."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed arena argv
            [*docker, "info", "--format", "{{.ServerVersion}}|{{json .SecurityOptions}}"],
            capture_output=True,
            text=True,
            timeout=_PROBE_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker not runnable: {type(exc).__name__}"
    version, _, options = proc.stdout.strip().partition("|")
    if proc.returncode != 0 or not version:
        return "docker daemon unreachable"
    if "name=seccomp" not in options:
        return "docker seccomp profile not active"
    return None


class DockerSandbox:
    def __init__(
        self, docker: Sequence[str] = ("docker",), image: str = DEFAULT_IMAGE, name_prefix: str = "duel-"
    ) -> None:
        self._docker = tuple(docker)
        self._image = image
        self._prefix = name_prefix  # containers are named <prefix><random>; tests use a per-run prefix

    def health(self) -> str | None:
        """Why this sandbox can't run challenges, or None. Probes the daemon, then runs a real self-test container."""
        problem = docker_probe(self._docker)
        if problem is not None:
            return problem
        result = self.run("print(FLAG)", "", _SELF_TEST_FLAG, SandboxLimits())
        if result.stdout.strip() != _SELF_TEST_FLAG:
            return f"self-test container failed (exit {result.exit_code}, killed={result.killed_reason})"
        return None

    def argv(self, name: str, limits: SandboxLimits) -> list[str]:
        """Container argv. Carries neither the flag nor the source: both go over stdin."""
        mem = f"{limits.memory_mb}m"
        return [
            *self._docker, "run", "--rm", "-i", "--pull", "never", "--name", name,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:size=1m,noexec,nosuid,nodev",  # noqa: S108 - container-private tmpfs
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--ipc", "none",
            "--user", "65534:65534",
            "--pids-limit", str(limits.pids_limit),
            "--memory", mem, "--memory-swap", mem,
            "--cpus", str(limits.cpus),
            "--ulimit", f"cpu={limits.cpu_seconds}:{limits.cpu_seconds}",
            "--ulimit", "core=0:0",
            "--log-driver", "none",
            "--stop-timeout", "1",
            self._image,
            "timeout", "--kill-after=1", str(limits.wall_seconds), "python", *PYTHON_FLAGS,
        ]  # fmt: skip

    def run(self, source: str, stdin: str, flag: str, limits: SandboxLimits) -> RunResult:
        name = f"{self._prefix}{uuid.uuid4().hex[:16]}"
        result = execute(
            self.argv(name, limits),
            frame(source, flag, stdin),
            limits,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            wall_seconds=limits.wall_seconds + STARTUP_GRACE_SECONDS,
            cleanup=lambda: self._remove(name),
        )
        if result.killed_reason:
            return result
        if result.exit_code == TIMEOUT_EXIT:
            return result.model_copy(update={"timed_out": True, "killed_reason": "wall"})
        if result.exit_code > SIGNAL_EXIT_BASE:
            return result.model_copy(update={"killed_reason": "signal"})
        return result

    def _remove(self, name: str) -> None:
        subprocess.run(  # noqa: S603 - fixed arena argv
            [*self._docker, "rm", "-f", name],
            capture_output=True,
            timeout=_PROBE_SECONDS,
            check=False,
        )
