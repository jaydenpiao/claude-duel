"""Shared flock'd, fsync'd append-only JSON-lines primitives (spend ledger, event log)."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path: Path, *, exclusive: bool) -> Iterator[int]:
    """Open path (creating it 0600) and hold a shared or exclusive flock for the block."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield fd
    finally:
        os.close(fd)  # closing releases the lock


def append(fd: int, line: bytes) -> None:
    """Append one newline-terminated line and fsync. Never glues onto a torn final line."""
    size = os.fstat(fd).st_size
    if size:
        os.lseek(fd, size - 1, os.SEEK_SET)
        if os.read(fd, 1) != b"\n":
            line = b"\n" + line
    os.lseek(fd, 0, os.SEEK_END)
    os.write(fd, line)
    os.fsync(fd)


def read_complete(fd: int, offset: int = 0) -> tuple[list[bytes], int]:
    """Complete lines from offset on, and the offset after the last one. A torn tail is left unread."""
    size = os.fstat(fd).st_size
    os.lseek(fd, offset, os.SEEK_SET)
    data = os.read(fd, max(size - offset, 0))
    complete = data[: data.rfind(b"\n") + 1]
    return [line for line in complete.splitlines() if line.strip()], offset + len(complete)


def truncate_torn_tail(fd: int, complete_end: int) -> None:
    """Drop bytes after the last complete line (a crash mid-write) and fsync, so the next append starts clean."""
    if os.fstat(fd).st_size > complete_end:
        os.ftruncate(fd, complete_end)
        os.fsync(fd)
