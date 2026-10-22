"""The single process runner used by every sandbox."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from typing import IO

from claude_duel.domain.models import SandboxLimits
from claude_duel.ports import RunResult

_CHUNK = 4096


def execute(
    argv: Sequence[str],
    stdin: str,
    limits: SandboxLimits,
    *,
    env: dict[str, str],
    wall_seconds: float | None = None,
    cwd: str | None = None,
    cleanup: Callable[[], object] | None = None,
) -> RunResult:
    """Run argv in its own process group with a wall-clock limit and a hard output cap.

    The whole process group is always killed afterwards, so forked children can't
    outlive the run; ``cleanup`` runs last (e.g. to remove a container).
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is built by the arena, never by a model
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    out = bytearray()
    overflow = threading.Event()
    reader = threading.Thread(target=_read_capped, args=(proc, proc.stdout, out, limits.max_output_bytes, overflow))
    reader.start()
    _write(proc.stdin, stdin.encode())
    timed_out = False
    try:
        proc.wait(timeout=wall_seconds if wall_seconds is not None else limits.wall_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        kill_group(proc.pid)
        proc.wait()
        reader.join()
        if cleanup is not None:
            cleanup()
    stdout = bytes(out[: limits.max_output_bytes]).decode(errors="replace")
    return RunResult(
        stdout=stdout, exit_code=proc.returncode, timed_out=timed_out, killed_reason=_reason(proc, timed_out, overflow)
    )


def kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _reason(proc: subprocess.Popen[bytes], timed_out: bool, overflow: threading.Event) -> str | None:
    if timed_out:
        return "wall"
    if overflow.is_set():
        return "output"
    return "signal" if proc.returncode < 0 else None


def _write(pipe: IO[bytes], data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.close()
    except (BrokenPipeError, OSError):
        return


def _read_capped(
    proc: subprocess.Popen[bytes], pipe: IO[bytes], out: bytearray, cap: int, overflow: threading.Event
) -> None:
    while chunk := pipe.read1(_CHUNK):  # type: ignore[attr-defined]
        out.extend(chunk)
        if len(out) > cap:
            overflow.set()
            kill_group(proc.pid)
            break
    pipe.close()
