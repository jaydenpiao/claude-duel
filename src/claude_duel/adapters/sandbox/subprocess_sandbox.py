"""Subprocess sandbox with POSIX rlimits. Defence in depth for the fake provider only (ADR 0003)."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable

from claude_duel.adapters.sandbox.common import execute
from claude_duel.adapters.sandbox.runner import HASH_ENV, PYTHON_FLAGS, frame
from claude_duel.domain.errors import SandboxRefusedError
from claude_duel.domain.models import SandboxLimits
from claude_duel.ports import RunResult


def rlimits(limits: SandboxLimits) -> dict[str, int]:
    mem = limits.memory_mb * 1024 * 1024
    return {
        "RLIMIT_CPU": limits.cpu_seconds,
        "RLIMIT_AS": mem,
        "RLIMIT_FSIZE": 0,
        "RLIMIT_NOFILE": 16,
        "RLIMIT_CORE": 0,
        # Per-UID limit: the arena user already runs more processes than this, so any fork fails.
        "RLIMIT_NPROC": 1,
    }


class SubprocessSandbox:
    """Not a security boundary. Runs only sources ``source_allowed`` accepts; the composition root passes
    the fake provider's ``is_template``, so model-written code can never reach it (ADR 0003).
    """

    def __init__(self, source_allowed: Callable[[str], bool], python: str = sys.executable) -> None:
        self._python = python
        self._source_allowed = source_allowed

    def run(self, source: str, stdin: str, flag: str, limits: SandboxLimits) -> RunResult:
        if not self._source_allowed(source):
            raise SandboxRefusedError("subprocess sandbox only runs fake-provider templates; use docker")
        with tempfile.TemporaryDirectory(prefix="duel-") as tmp:
            os.chmod(tmp, 0o500)  # read-only, empty working directory
            return execute(
                [self._python, *PYTHON_FLAGS],
                frame(source, flag, stdin, rlimits(limits)),
                limits,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", **HASH_ENV},
                cwd=tmp,
            )
