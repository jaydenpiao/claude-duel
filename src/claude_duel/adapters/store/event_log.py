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
from claude_duel.ports import EventLog, EventObserver

GENESIS = "0" * 64


def _digest(prev: str, event: Event) -> str:
    body = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


class FileEventLog:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, match_id: str) -> Path:
        if not match_id.replace("-", "").isalnum():
            raise MatchNotFoundError(f"invalid match id {match_id!r}")
        return self._root / f"{match_id}.events.jsonl"

    def append(self, match_id: str, event: Event) -> None:
        with jsonl.locked(self._path(match_id), exclusive=True) as fd:
            lines, _ = jsonl.read_complete(fd)
            events, prev = _verify(lines)
            expected = events[-1].seq + 1 if events else 0
            if event.seq != expected:
                raise EventLogCorruptError(f"event seq {event.seq} != expected {expected}")
            record = {"prev": prev, "hash": _digest(prev, event), "event": event.model_dump(mode="json")}
            jsonl.append(fd, (json.dumps(record, sort_keys=True) + "\n").encode())

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


class ObservedEventLog:
    """EventLog decorator: notifies an observer after each successful append. Observers never affect the match."""

    def __init__(self, inner: EventLog, observer: EventObserver) -> None:
        self._inner = inner
        self._observer = observer

    def append(self, match_id: str, event: Event) -> None:
        self._inner.append(match_id, event)
        self._observer(event)

    def read(self, match_id: str) -> list[Event]:
        return self._inner.read(match_id)
