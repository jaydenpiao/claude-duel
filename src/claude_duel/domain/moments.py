"""Moment detector (design-deep-layer.md, phase 1): named highlights derived purely from the event log.

Phase 1 knows one kind, ``GHOST_STRIKE``: a past capture's payload took a new flag at $0. Moments carry ids only,
never payloads or flags, so views can show them for any challenge.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from claude_duel.domain import events as ev
from claude_duel.domain.models import Event

GHOST_STRIKE = "GHOST_STRIKE"


@dataclass(frozen=True)
class Moment:
    kind: str
    round: int
    seq: int
    subject: str  # the challenge the moment is about
    by: str  # the ghost id, "<challenge id>:<attacker>"


def moment_of(event: Event) -> Moment | None:
    if event.type == ev.GHOST_RUN and event.data.get("hit"):
        d = event.data
        return Moment(GHOST_STRIKE, cast("int", d["round"]), event.seq, str(d["target"]), str(d["ghost_id"]))
    return None


def moments(events: Iterable[Event]) -> list[Moment]:
    return [m for m in map(moment_of, events) if m is not None]
