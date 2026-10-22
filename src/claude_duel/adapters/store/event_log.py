"""Per-match append-only, hash-chained event log (``<match_id>.events.jsonl``), ADR 0004.

Each line is ``{"prev": <hash>, "hash": sha256(prev + event), "event": {...}}``. Reading
verifies the chain, so any edit, deletion or reordering is detected.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from claude_duel.adapters.store import jsonl
from claude_duel.domain.errors import EventLogCorruptError, MatchNotFoundError
from claude_duel.domain.models import Event

GENESIS = "0" * 64


def _wire(event: Event) -> dict[str, object]:
    """JSON form of an event; pre-versioning events (v=0) keep their original shape so old hashes verify."""
    return event.model_dump(mode="json", exclude={"v"} if event.v == 0 else None)


def _digest(prev: str, event: Event) -> str:
    body = json.dumps(_wire(event), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


class FileEventLog:
    def __init__(self, root: Path, suffix: str = ".events.jsonl") -> None:
        self._root, self._suffix = root, suffix
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, match_id: str) -> Path:
        if not match_id.replace("-", "").isalnum():
            raise MatchNotFoundError(f"invalid match id {match_id!r}")
        return self._root / f"{match_id}{self._suffix}"

    def append(self, match_id: str, event: Event) -> None:
        with jsonl.locked(self._path(match_id), exclusive=True) as fd:
            lines, end = jsonl.read_complete(fd)
            jsonl.truncate_torn_tail(fd, end)  # round-2 N1: a torn, never-acknowledged write is dropped
            events, prev = _verify(lines)
            expected = events[-1].seq + 1 if events else 0
            if event.seq != expected:
                raise EventLogCorruptError(f"event seq {event.seq} != expected {expected}")
            if event.seq == 0 and event.type == "match_created" and "created_seq" not in event.data:
                event = event.model_copy(update={"data": {**event.data, "created_seq": self._next_created_seq()}})
            record = {"prev": prev, "hash": _digest(prev, event), "event": _wire(event)}
            jsonl.append(fd, (json.dumps(record, sort_keys=True) + "\n").encode())

    def _next_created_seq(self) -> int:
        """Monotonic match-creation counter for this store (#137): Elo folds matches in this order."""
        with jsonl.locked(self._root / ".created_seq", exclusive=True) as fd:
            lines, _ = jsonl.read_complete(fd)
            n = int(lines[-1]) + 1 if lines else 0
            jsonl.append(fd, f"{n}\n".encode())
        return n

    def ids(self) -> list[str]:
        """Every log id in the store, sorted."""
        return sorted(p.name[: -len(self._suffix)] for p in self._root.glob(f"*{self._suffix}"))

    def read(self, match_id: str) -> list[Event]:
        path = self._path(match_id)
        if not path.exists():
            return []
        with jsonl.locked(path, exclusive=False) as fd:
            lines, _ = jsonl.read_complete(fd)
        return _verify(lines)[0]


def _verify(lines: list[bytes]) -> tuple[list[Event], str]:
    events: list[Event] = []
    prev = GENESIS
    for n, raw in enumerate(lines):
        try:
            record = json.loads(raw)
            event = Event.model_validate(record["event"])
        except (ValueError, KeyError, TypeError) as exc:
            raise EventLogCorruptError(f"unreadable event line {n}") from exc
        if record.get("prev") != prev or record.get("hash") != _digest(prev, event) or event.seq != n:
            raise EventLogCorruptError(f"hash chain broken at event {n}")
        events.append(event)
        prev = record["hash"]
    return events, prev
