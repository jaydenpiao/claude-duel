"""Persistent append-only spend ledger (JSON lines), shared by every match, run and process.

``reserve`` appends a worst-case entry before a provider call; the charge that follows carries
``settles`` and cancels it. A crash in between leaves the reservation counted (over, never under).

All reads and appends hold ``flock`` on the ledger file, so totals are consistent
across concurrent processes. Readers fold only the bytes appended since their last read.
A reservation's cap check and its append happen under one exclusive lock, so racers cannot both pass.

A line that cannot be trusted (torn, unparseable, or a negative or non-finite ``usd``) raises
``SpendLedgerCorruptError``: spending stops until a person inspects the file, instead of the line being skipped or
lowering the total.

Idempotent attempts: a reservation and its charge may carry an attempt ``key``. A charge with a ``reply`` keeps that
reply under ``replies/`` (0600, written before the charge line), so a resume after a crash between the charge and the
match's own event reuses it instead of paying again; a second reservation for the same key replaces the first.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from claude_duel.adapters.store import jsonl
from claude_duel.domain.errors import BudgetExceededError, SpendLedgerCorruptError


class FileSpendLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._offset = 0
        self._total = 0.0
        self._open: dict[str, tuple[str, float]] = {}  # reservation id -> (match id, usd)
        self._by_match: dict[str, float] = {}
        self._keys: dict[str, str] = {}  # attempt key -> its open reservation id
        self._charged: set[str] = set()  # attempt keys with a settled charge
        self._replies = path.parent / "replies"

    def spent_usd(self, prefix: str) -> float:
        """Spend of every match whose id starts with ``prefix`` (a series or tournament), failed games included."""
        with jsonl.locked(self._path, exclusive=False) as fd:
            self._catch_up(fd)
        return self._spent(prefix)

    def total_usd(self) -> float:
        """Lifetime spend as currently on disk, including other processes' entries."""
        with jsonl.locked(self._path, exclusive=False) as fd:
            self._catch_up(fd)
        return self._total

    def reserve(
        self,
        match_id: str,
        model: str,
        usd: float,
        caps: Sequence[tuple[str, str, float]] = (),
        key: str | None = None,
    ) -> str:
        """Count ``usd`` now, before the call; ``record(..., settles=id)`` replaces it with the real cost. The cap check
        reads spend and appends under one exclusive lock. A stale open reservation for ``key`` is released first."""
        rid = uuid.uuid4().hex
        with jsonl.locked(self._path, exclusive=True) as fd:
            self._catch_up(fd)
            stale = self._keys.get(key) if key else None
            stale_mid, stale_usd = self._open.get(stale, ("", 0.0)) if stale else ("", 0.0)
            for label, prefix, max_usd in caps:
                spent = self._spent(prefix) - (stale_usd if stale and stale_mid.startswith(prefix) else 0.0)
                if spent + usd > max_usd:
                    raise BudgetExceededError(
                        f"hard stop: {label} spent ${spent:.4f} + this call's worst case ${usd:.4f}"
                        f" > ${max_usd:.2f}; nothing was called, resume after raising the cap"
                    )
            now = time.time()
            if stale:  # the same attempt again after a crash: one attempt holds one reservation
                release = {"ts": now, "match_id": stale_mid, "model": model, "tokens": 0, "usd": 0.0, "settles": stale}
                jsonl.append(fd, (json.dumps(release) + "\n").encode())
            entry: dict[str, object] = {
                "ts": now,
                "match_id": match_id,
                "model": model,
                "tokens": 0,
                "usd": usd,
                "reserve": rid,
            }
            if key:
                entry["key"] = key
            jsonl.append(fd, (json.dumps(entry) + "\n").encode())
            self._catch_up(fd)
        return rid

    def record(
        self,
        match_id: str,
        model: str,
        tokens: int,
        usd: float,
        settles: str | None = None,
        key: str | None = None,
        reply: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, object] = {
            "ts": time.time(),
            "match_id": match_id,
            "model": model,
            "tokens": tokens,
            "usd": usd,
        }
        if settles:
            entry["settles"] = settles
        if key:
            entry["key"] = key
            if reply is not None:
                self._keep(key, reply)
        self._append(entry)

    def settled(self, key: str) -> dict[str, Any] | None:
        with jsonl.locked(self._path, exclusive=False) as fd:
            self._catch_up(fd)
        if key not in self._charged:
            return None
        try:
            data = json.loads(self._reply_path(key).read_text())
        except (FileNotFoundError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def forget(self, key: str) -> None:
        self._reply_path(key).unlink(missing_ok=True)

    def _reply_path(self, key: str) -> Path:
        if not key.isalnum():
            raise ValueError("attempt keys are hex digests")
        return self._replies / f"{key}.json"

    def _keep(self, key: str, reply: dict[str, Any]) -> None:
        """Write the reply atomically at 0600 and fsync it before the charge line refers to it."""
        self._replies.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._reply_path(key)
        tmp = path.with_name(f".{key}.{uuid.uuid4().hex}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps(reply).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _spent(self, prefix: str) -> float:
        return sum(v for k, v in self._by_match.items() if k.startswith(prefix))

    def _append(self, entry: dict[str, object]) -> None:
        with jsonl.locked(self._path, exclusive=True) as fd:
            jsonl.append(fd, (json.dumps(entry) + "\n").encode())
            self._catch_up(fd)

    def record_override(self, run_id: str, value: float, reason: str) -> None:
        """A $0 audit row for a run-cap override; it never adds to the total."""
        entry = {"ts": time.time(), "kind": "override", "run_id": run_id, "value": value, "reason": reason, "usd": 0.0}
        with jsonl.locked(self._path, exclusive=True) as fd:
            jsonl.append(fd, (json.dumps(entry) + "\n").encode())
            self._catch_up(fd)

    def _catch_up(self, fd: int) -> None:
        if os.fstat(fd).st_size < self._offset:  # truncated or replaced: refold from the start
            self._offset, self._total, self._open, self._by_match = 0, 0.0, {}, {}
            self._keys, self._charged = {}, set()
        start = self._offset
        lines, end = jsonl.read_complete(fd, self._offset)
        if os.fstat(fd).st_size > end:  # appends are whole lines under the exclusive lock: this is crash residue
            raise SpendLedgerCorruptError(f"spend ledger {self._path} has a torn last line; inspect it, then resume")
        for line in lines:
            entry = _entry(line)
            if entry is None:
                raise SpendLedgerCorruptError(
                    f"spend ledger {self._path} has an unreadable or negative/non-finite usd line after byte {start};"
                    " spending stops until it is inspected"
                )
            mid = str(entry.get("match_id", ""))
            usd = float(entry["usd"])
            settles = str(entry.get("settles", ""))
            _, held = self._open.pop(settles, ("", 0.0))
            self._total += usd - held
            self._by_match[mid] = self._by_match.get(mid, 0.0) + usd - held
            key = entry.get("key")
            if "reserve" in entry:
                self._open[str(entry["reserve"])] = (mid, usd)
                if key:
                    self._keys[str(key)] = str(entry["reserve"])
            elif key:
                self._charged.add(str(key))
                self._keys.pop(str(key), None)
            elif settles:
                self._keys = {k: r for k, r in self._keys.items() if r != settles}
        self._offset = end


def _entry(raw: bytes) -> dict[str, Any] | None:
    """A trusted ledger line, or None: unparseable, no usd, or a negative or non-finite usd."""
    try:
        entry = json.loads(raw)
        usd = float(entry["usd"])
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(entry, dict) or not math.isfinite(usd) or usd < 0:
        return None
    return entry
