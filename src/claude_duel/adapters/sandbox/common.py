"""Shared sandbox helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from claude_duel.domain.models import SandboxLimits
from claude_duel.ports import RunResult


def execute(
    argv: list[str],
    stdin: str,
    env: dict[str, str],
    limits: SandboxLimits,
    cwd: str | None = None,
    preexec: Callable[[], object] | None = None,
) -> RunResult:
    """Run argv with a wall-clock limit and bounded output."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built by the arena, never by a model
            argv,
            input=stdin.encode(),
            capture_output=True,
            env=env,
            cwd=cwd,
            timeout=limits.wall_seconds,
            preexec_fn=preexec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"")[: limits.max_output_bytes]
        return RunResult(stdout=out.decode(errors="replace"), exit_code=-9, timed_out=True, killed_reason="wall")
    stdout = proc.stdout[: limits.max_output_bytes].decode(errors="replace")
    killed = "signal" if proc.returncode < 0 else None
    return RunResult(stdout=stdout, exit_code=proc.returncode, timed_out=False, killed_reason=killed)
