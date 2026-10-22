"""Subprocess sandbox with POSIX rlimits. Defence in depth, not a hard boundary (see ADR 0003)."""

from __future__ import annotations

import os
import resource
import sys
import tempfile
from collections.abc import Callable, Mapping

from claude_duel.adapters.sandbox.common import execute
from claude_duel.domain.models import SandboxLimits
from claude_duel.ports import RunResult


def _limiter(limits: SandboxLimits) -> Callable[[], None]:
    mem = limits.memory_mb * 1024 * 1024

    def apply() -> None:  # pragma: no cover - runs in the child process
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


class SubprocessSandbox:
    def __init__(self, python: str = sys.executable) -> None:
        self._python = python

    def run(self, source: str, stdin: str, env: Mapping[str, str], limits: SandboxLimits) -> RunResult:
        with tempfile.TemporaryDirectory(prefix="duel-") as tmp:
            path = os.path.join(tmp, "challenge.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)
            os.chmod(tmp, 0o500)
            child_env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", **env}
            return execute(
                [self._python, "-I", "-S", path], stdin, child_env, limits, cwd=tmp, preexec=_limiter(limits)
            )
