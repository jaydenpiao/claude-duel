"""Persistent append-only spend ledger (JSON lines), shared by every match, run and process.

All reads and appends hold ``flock`` on the ledger file, so totals are consistent
across concurrent processes. Readers fold only the bytes appended since their last read.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from claude_duel.adapters.store import jsonl


class FileSpendLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._offset = 0
        self._total = 0.0
        self._by_match: dict[str, float] = {}

    def spent_usd(self, prefix: str) -> float:
        """Spend of every match whose id starts with ``prefix`` (a series or tournament), failed games included."""
        with jsonl.locked(self._path, exclusive=False) as fd:
            self._catch_up(fd)
        return sum(v for k, v in self._by_match.items() if k.startswith(prefix))

    def total_usd(self) -> float:
        """Lifetime spend as currently on disk, including other processes' entries."""
        with jsonl.locked(self._path, exclusive=False) as fd:
            self._catch_up(fd)
        return self._total

    def record(self, match_id: str, model: str, tokens: int, usd: float) -> None:
        entry = {"ts": time.time(), "match_id": match_id, "model": model, "tokens": tokens, "usd": usd}
        with jsonl.locked(self._path, exclusive=True) as fd:
            jsonl.append(fd, (json.dumps(entry) + "\n").encode())
            self._catch_up(fd)

    def _catch_up(self, fd: int) -> None:
        if os.fstat(fd).st_size < self._offset:  # truncated or replaced: refold from the start
            self._offset, self._total, self._by_match = 0, 0.0, {}
        lines, self._offset = jsonl.read_complete(fd, self._offset)
        for line in lines:
            mid, usd = _entry(line)
            self._total += usd
            self._by_match[mid] = self._by_match.get(mid, 0.0) + usd


def _entry(raw: bytes) -> tuple[str, float]:
    try:
        row = json.loads(raw)
        return str(row.get("match_id", "")), float(row["usd"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return "", 0.0  # torn line from a crash; never counted
