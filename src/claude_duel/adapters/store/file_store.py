"""Atomic JSON-file match store with per-match lock files."""

from __future__ import annotations

import os
from pathlib import Path

from claude_duel.domain.errors import MatchLockedError, MatchNotFoundError
from claude_duel.domain.models import MatchState


class FileLock:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> FileLock:
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise MatchLockedError(f"{self._path.name} is held by another process") from exc
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc: object) -> None:
        self._path.unlink(missing_ok=True)


class FileMatchStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, match_id: str) -> Path:
        if not match_id.replace("-", "").isalnum():
            raise MatchNotFoundError(f"invalid match id {match_id!r}")
        return self._root / f"{match_id}.json"

    def save(self, state: MatchState) -> None:
        path = self._path(state.match_id)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(state.model_dump_json(indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def load(self, match_id: str) -> MatchState:
        path = self._path(match_id)
        if not path.exists():
            raise MatchNotFoundError(match_id)
        return MatchState.model_validate_json(path.read_text(encoding="utf-8"))

    def lock(self, match_id: str) -> FileLock:
        return FileLock(self._path(match_id).with_suffix(".lock"))
