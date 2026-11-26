"""Durable provider replies by attempt key (``ReplyStore``): one 0600 file per key under ``replies/``.

A reply is written atomically and fsynced before the ledger charge line that refers to it, so a resume after a crash
between the charge and the match's own event reuses it. Keys are hex digests; the money lives in the spend ledger.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


class FileReplyStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def keep(self, key: str, reply: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(key)
        tmp = path.with_name(f".{key}.{uuid.uuid4().hex}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps(reply).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(key).read_text())
        except (FileNotFoundError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def forget(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        if not key.isalnum():
            raise ValueError("attempt keys are hex digests")
        return self._dir / f"{key}.json"
