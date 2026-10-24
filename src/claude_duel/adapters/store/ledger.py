"""Persistent append-only spend ledger (JSON lines), shared by every match, run and process.

``reserve`` appends a worst-case entry before a provider call; the charge that follows carries
``settles`` and cancels it. A crash in between leaves the reservation counted (over, never under).

All reads and appends hold ``flock`` on the ledger file, so totals are consistent
across concurrent processes. Readers fold only the bytes appended since their last read.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from claude_duel.adapters.store import jsonl


class FileSpendLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._offset = 0
        self._total = 0.0
        self._open: dict[str, tuple[str, float]] = {}  # reservation id -> (match id, usd)
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

    def reserve(self, match_id: str, model: str, usd: float) -> str:
        """Count ``usd`` now, before the call; ``record(..., settles=id)`` replaces it with the real cost."""
        rid = uuid.uuid4().hex
        self._append({"ts": time.time(), "match_id": match_id, "model": model, "tokens": 0, "usd": usd, "reserve": rid})
        return rid

    def record(self, match_id: str, model: str, tokens: int, usd: float, settles: str | None = None) -> None:
        entry: dict[str, object] = {
            "ts": time.time(),
            "match_id": match_id,
            "model": model,
            "tokens": tokens,
            "usd": usd,
        }
        if settles:
            entry["settles"] = settles
        self._append(entry)

    def _append(self, entry: dict[str, object]) -> None:
        with jsonl.locked(self._path, exclusive=True) as fd:
            jsonl.append(fd, (json.dumps(entry) + "\n").encode())
            self._catch_up(fd)

    def _catch_up(self, fd: int) -> None:
        if os.fstat(fd).st_size < self._offset:  # truncated or replaced: refold from the start
            self._offset, self._total, self._open, self._by_match = 0, 0.0, {}, {}
        lines, self._offset = jsonl.read_complete(fd, self._offset)
        for line in lines:
            entry = _entry(line)
            if entry is None:
                continue  # torn line from a crash; never counted
            mid = str(entry.get("match_id", ""))
            usd = float(entry["usd"])
            _, held = self._open.pop(str(entry.get("settles", "")), ("", 0.0))
            self._total += usd - held
            self._by_match[mid] = self._by_match.get(mid, 0.0) + usd - held
            if "reserve" in entry:
                self._open[str(entry["reserve"])] = (mid, usd)


def _entry(raw: bytes) -> dict[str, Any] | None:
    try:
        entry = json.loads(raw)
        float(entry["usd"])
    except (ValueError, KeyError, TypeError):
        return None
    return entry if isinstance(entry, dict) else None
