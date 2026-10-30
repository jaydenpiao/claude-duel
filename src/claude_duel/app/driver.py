"""Shared helpers for the series and tournament drivers (event-log open/append, spend caps, pinned rules)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from claude_duel.app.container import build_ledger
from claude_duel.config import Settings
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import Event, Rules, SandboxLimits
from claude_duel.observability import log
from claude_duel.ports import EventLog

_LOG = logging.getLogger(__name__)


def open_or_create(
    event_log: EventLog,
    log_id: str,
    *,
    schema_version: int,
    created_type: str,
    data: Mapping[str, Any],
) -> list[Event]:
    """Return the log's events, writing a created event when the log is empty."""
    events = event_log.read(log_id)
    if not events:
        events = [
            Event(v=schema_version, seq=0, type=created_type, trace_id=log_id, data=dict(data)),
        ]
        event_log.append(log_id, events[0])
    return events


def append(
    event_log: EventLog,
    log_id: str,
    *,
    schema_version: int,
    seq: int,
    event_type: str,
    data: Mapping[str, Any],
) -> Event:
    """Append one event with ``seq`` and return it."""
    event = Event(v=schema_version, seq=seq, type=event_type, trace_id=log_id, data=dict(data))
    event_log.append(log_id, event)
    return event


def pinned(settings: Settings, rules: Rules | None, limits: SandboxLimits | None, log_id: str) -> Settings:
    """Settings with the rules and limits pinned when the driver log was created; warns on config drift."""
    rules, limits = rules or settings.rules, limits or settings.limits
    if (rules, limits) != (settings.rules, settings.limits):
        log(
            _LOG,
            logging.WARNING,
            "config differs from the saved driver log; resuming with the saved rules",
            id=log_id,
        )
    return settings.model_copy(update={"rules": rules, "limits": limits})


@dataclass(frozen=True)
class SpendCap:
    """A spend ceiling over every match whose id starts with ``prefix``, read from the shared ledger."""

    label: str
    prefix: str
    max_usd: float


def check_caps(settings: Settings, caps: tuple[SpendCap, ...]) -> None:
    ledger = build_ledger(settings)
    for cap in caps:
        spent = ledger.spent_usd(cap.prefix)
        if spent >= cap.max_usd:
            raise BudgetExceededError(f"{cap.label} cost ${spent:.4f}/${cap.max_usd}")
