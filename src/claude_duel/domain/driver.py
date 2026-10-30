"""Shared fold header for series and tournament logs."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

from pydantic import BaseModel

from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.events import check_version, typed
from claude_duel.domain.models import Event

_S = TypeVar("_S", bound=BaseModel)


def fold_created(
    events: Iterable[Event],
    *,
    created_type: str,
    schema_version: int,
    what: str,
    state_type: type[_S],
    id_field: str,
    id_value: str,
) -> tuple[_S, Iterator[Event]]:
    """Validate the created event and return ``(state, remaining_events)``."""
    it = iter(events)
    first = next(it, None)
    if first is None or first.type != created_type:
        raise EventLogCorruptError(f"{what} log does not start with {created_type}")
    check_version(first, schema_version, f"{what} event")
    state = typed(state_type, {id_field: id_value, **first.data})
    return state, it
