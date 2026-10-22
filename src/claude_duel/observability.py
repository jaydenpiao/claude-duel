"""Structured JSON logging with redaction, trace ids and metrics hooks."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

from claude_duel.domain.flags import redact

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_SECRET_PATTERNS = (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), re.compile(r"(?i)(api[_-]?key|authorization)[\"':= ]+\S+"))


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    return _trace_id.get()


@contextmanager
def trace(trace_id: str | None = None) -> Iterator[str]:
    token = _trace_id.set(trace_id or new_trace_id())
    try:
        yield _trace_id.get()
    finally:
        _trace_id.reset(token)


def scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("<redacted-secret>", text)
    return redact(text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": current_trace_id(),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return scrub(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("claude_duel")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    logger.log(level, msg, extra={"fields": fields})


class NullMetrics:
    def incr(self, name: str, value: int = 1, **tags: str) -> None:
        return None

    def timing(self, name: str, seconds: float, **tags: str) -> None:
        return None


class InMemoryMetrics:
    def __init__(self) -> None:
        self.counters: dict[str, int] = defaultdict(int)
        self.timings: dict[str, list[float]] = defaultdict(list)

    def incr(self, name: str, value: int = 1, **tags: str) -> None:
        self.counters[name] += value

    def timing(self, name: str, seconds: float, **tags: str) -> None:
        self.timings[name].append(seconds)
