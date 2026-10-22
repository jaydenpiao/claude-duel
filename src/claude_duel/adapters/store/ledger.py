"""Persistent append-only spend ledger (JSON lines), shared by every match, run and process.

All reads and appends happen under ``flock`` on the ledger file, so totals are
consistent across concurrent processes. Readers fold only the bytes appended
since their last read.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


class FileSpendLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._offset = 0
        self._total = 0.0

    def total_usd(self) -> float:
        """Lifetime spend as currently on disk, including other processes' entries."""
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            self._catch_up(fd)
        finally:
            os.close(fd)
        return self._total

    def record(self, match_id: str, model: str, tokens: int, usd: float) -> None:
        entry = {"ts": time.time(), "match_id": match_id, "model": model, "tokens": tokens, "usd": usd}
        line = (json.dumps(entry) + "\n").encode()
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if _ends_torn(fd):
                line = b"\n" + line  # never glue a new entry onto a torn final line
            os.lseek(fd, 0, os.SEEK_END)
            os.write(fd, line)
            os.fsync(fd)
            self._catch_up(fd)
        finally:
            os.close(fd)

    def _open(self) -> int:
        return os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)

    def _catch_up(self, fd: int) -> None:
        size = os.fstat(fd).st_size
        if size < self._offset:  # truncated or replaced: refold from the start
            self._offset, self._total = 0, 0.0
        os.lseek(fd, self._offset, os.SEEK_SET)
        data = os.read(fd, size - self._offset)
        complete = data[: data.rfind(b"\n") + 1]  # a torn tail is re-read once completed
        for raw in complete.splitlines():
            self._total += _usd(raw)
        self._offset += len(complete)


def _usd(raw: bytes) -> float:
    try:
        return float(json.loads(raw)["usd"])
    except (ValueError, KeyError, TypeError):
        return 0.0  # torn line from a crash; never counted


def _ends_torn(fd: int) -> bool:
    size = os.fstat(fd).st_size
    if size == 0:
        return False
    os.lseek(fd, size - 1, os.SEEK_SET)
    return os.read(fd, 1) != b"\n"
