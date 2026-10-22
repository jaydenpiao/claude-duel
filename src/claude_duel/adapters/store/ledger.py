"""Persistent append-only spend ledger (JSON lines), shared across matches and runs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class FileSpendLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._total = self._load()

    def _load(self) -> float:
        if not self._path.exists():
            return 0.0
        total = 0.0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                total += float(json.loads(line)["usd"])
            except (ValueError, KeyError, TypeError):
                continue  # torn final line after a crash; ignore
        return total

    def total_usd(self) -> float:
        return self._total

    def record(self, match_id: str, model: str, tokens: int, usd: float) -> None:
        entry = {"ts": time.time(), "match_id": match_id, "model": model, "tokens": tokens, "usd": usd}
        fd = os.open(self._path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._total += usd
