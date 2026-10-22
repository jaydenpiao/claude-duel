"""Metrics sink (#11): one JSON line per counter or timing, appended to ``metrics.jsonl``."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

from claude_duel.adapters.store import jsonl
from claude_duel.domain.flags import scrub
from claude_duel.observability import current_trace_id
from claude_duel.ports import Metrics


class JsonlMetrics:
    """Appends under the shared flock, so concurrent processes interleave whole lines."""

    def __init__(self, path: Path, match_id: str) -> None:
        self._path = path
        self._match_id = match_id
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def incr(self, name: str, value: int = 1, **tags: str) -> None:
        self._write("counter", name, value, tags)

    def timing(self, name: str, seconds: float, **tags: str) -> None:
        self._write("timing", name, round(seconds, 6), tags)

    def _write(self, kind: str, name: str, value: float, tags: dict[str, str]) -> None:
        entry = {
            "ts": time.time(),
            "match_id": self._match_id,
            "trace_id": current_trace_id(),
            "kind": kind,
            "name": name,
            "value": value,
            "tags": dict(sorted(tags.items())),
        }
        with jsonl.locked(self._path, exclusive=True) as fd:
            jsonl.append(fd, (scrub(json.dumps(entry)) + "\n").encode())


class TeeMetrics:
    """Sends every metric to each sink (e.g. the in-memory summary plus a file)."""

    def __init__(self, sinks: Sequence[Metrics]) -> None:
        self._sinks = tuple(sinks)

    def incr(self, name: str, value: int = 1, **tags: str) -> None:
        for sink in self._sinks:
            sink.incr(name, value, **tags)

    def timing(self, name: str, seconds: float, **tags: str) -> None:
        for sink in self._sinks:
            sink.timing(name, seconds, **tags)
