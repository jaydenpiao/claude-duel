"""Docker sandbox: no network, read-only rootfs, memory/pids/cpu limits."""

from __future__ import annotations

import shutil
from collections.abc import Mapping

from claude_duel.adapters.sandbox.common import execute
from claude_duel.domain.models import SandboxLimits
from claude_duel.ports import RunResult


def docker_available() -> bool:
    return shutil.which("docker") is not None


class DockerSandbox:
    def __init__(self, image: str = "python:3.12-slim", docker: str = "docker") -> None:
        self._image = image
        self._docker = docker

    def argv(self, source: str, env: Mapping[str, str], limits: SandboxLimits) -> list[str]:
        argv = [
            self._docker,
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            f"{limits.memory_mb}m",
            "--cpus",
            "0.5",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:size=1m",  # noqa: S108 - container-private tmpfs
        ]
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
        return [*argv, self._image, "python", "-I", "-S", "-c", source]

    def run(self, source: str, stdin: str, env: Mapping[str, str], limits: SandboxLimits) -> RunResult:
        # Container start-up dominates; allow headroom over the configured wall time.
        padded = limits.model_copy(update={"wall_seconds": limits.wall_seconds + 10})
        return execute(self.argv(source, env, limits), stdin, {"PATH": "/usr/bin:/bin:/usr/local/bin"}, padded)
