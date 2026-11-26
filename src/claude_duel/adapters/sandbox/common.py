"""The single process runner used by every sandbox."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from typing import IO

from claude_duel.domain.models import SandboxLimits
from claude_duel.ports import KILL_OUTPUT, KILL_SIGNAL, KILL_WALL, KilledReason, RunResult

_CHUNK = 4096
# After the group kill and cleanup, how long to wait for the client process and the stdout reader. A privileged
# client (``sudo -n docker``) survives killpg, so nothing here may wait on it without a bound.
REAP_SECONDS = 5.0


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

    The whole process group is always killed afterwards, so forked children can't outlive the run. ``cleanup`` (e.g.
    ``docker rm -f``) runs right after the kill and before any wait on the client or its stdout reader, because a
    privileged client survives the group kill and keeps the pipe open until its container is gone. Every wait after
    that is bounded by ``REAP_SECONDS``.
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
    reader = threading.Thread(
        target=_read_capped, args=(proc, proc.stdout, out, limits.max_output_bytes, overflow), daemon=True
    )
    reader.start()
    _write(proc.stdin, stdin.encode())
    timed_out = False
    try:
        proc.wait(timeout=wall_seconds if wall_seconds is not None else limits.wall_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        kill_group(proc.pid)
        if cleanup is not None:
            cleanup()
        _reap(proc, reader)
    stdout = bytes(out[: limits.max_output_bytes]).decode(errors="replace")
    return RunResult(
        stdout=stdout, exit_code=proc.returncode, timed_out=timed_out, killed_reason=_reason(proc, timed_out, overflow)
    )


def _reap(proc: subprocess.Popen[bytes], reader: threading.Thread) -> None:
    """Bounded waits only: a client that outlives the kill and cleanup is abandoned, never waited on forever."""
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=REAP_SECONDS)
    reader.join(timeout=REAP_SECONDS)


def kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _reason(proc: subprocess.Popen[bytes], timed_out: bool, overflow: threading.Event) -> KilledReason | None:
    if timed_out:
        return KILL_WALL
    if overflow.is_set():
        return KILL_OUTPUT
    return KILL_SIGNAL if proc.returncode < 0 else None


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
