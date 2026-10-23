"""Atomic JSON-file match store with per-match lock files."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from claude_duel.domain.errors import MatchLockedError, MatchNotFoundError
from claude_duel.domain.models import MatchState


class FileLock:
    """Non-blocking ``flock`` on a held fd. The kernel drops it when the holder dies, so locks never go stale.

    The lock file is never unlinked: deleting it would let two processes lock different inodes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        if self._fd is not None:
            raise MatchLockedError(f"{self._path.name} is already held by this lock")
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise MatchLockedError(f"{self._path.name} is held by another process") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())  # diagnostics only; the flock is the lock
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)  # releases the flock
            self._fd = None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class FileMatchStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, match_id: str) -> Path:
        if not match_id.replace("-", "").isalnum():
            raise MatchNotFoundError(f"invalid match id {match_id!r}")
        return self._root / f"{match_id}.json"

    def save(self, state: MatchState) -> None:
        """State goes to ``<id>.json``; the HMAC secret only ever to ``<id>.secret`` (0600, written once)."""
        path = self._path(state.match_id)
        secret = path.with_suffix(".secret")
        if state.secret and not secret.exists():
            _write_atomic(secret, state.secret)
        _write_atomic(path, state.model_dump_json(indent=1))  # MatchState.secret is excluded from dumps

    def load(self, match_id: str) -> MatchState:
        path = self._path(match_id)
        if not path.exists():
            raise MatchNotFoundError(match_id)
        state = MatchState.model_validate_json(path.read_text(encoding="utf-8"))
        secret = path.with_suffix(".secret")
        if secret.exists():
            state.secret = secret.read_text(encoding="utf-8")
        if not state.secret:  # an older file carries it inline (moved out on next save); otherwise it is lost
            raise MatchNotFoundError(f"{match_id}: match secret missing ({secret.name})")
        return state

    def lock(self, match_id: str) -> FileLock:
        return FileLock(self._path(match_id).with_suffix(".lock"))
