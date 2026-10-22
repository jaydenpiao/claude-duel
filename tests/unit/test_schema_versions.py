"""#139: match events are schema v2; series and tournament events carry their own version and typed payloads."""

from __future__ import annotations

import pytest

from claude_duel.domain import events as ev
from claude_duel.domain import series as sr
from claude_duel.domain import tournament as tr
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import AgentSpec, Event


def _created(kind: str, data: dict[str, object], v: int = 1) -> Event:
    return Event(v=v, seq=0, type=kind, trace_id="t", data=data)


def test_match_schema_is_v2_and_newer_is_refused() -> None:
    assert ev.SCHEMA_VERSION == 2
    with pytest.raises(EventLogCorruptError, match="v3"):
        ev.payload(Event(v=3, seq=0, type=ev.NOTE_RECORDED, trace_id="t", data={}))


def test_tournament_version_and_typed_payloads() -> None:
    rule = tr.TournamentRule(entrants=tuple(AgentSpec(name=f"e{i}") for i in range(3)))
    data: dict[str, object] = {"seed": "s", "rule": rule.model_dump(mode="json")}
    assert tr.fold("t", [_created(tr.TOURNAMENT_CREATED, data)]).pairings == []
    with pytest.raises(EventLogCorruptError, match="tournament event 0 uses schema v2"):
        tr.fold("t", [_created(tr.TOURNAMENT_CREATED, data, v=tr.TOURNAMENT_SCHEMA_VERSION + 1)])
    with pytest.raises(EventLogCorruptError, match="TournamentState"):
        tr.fold("t", [_created(tr.TOURNAMENT_CREATED, {**data, "junk": 1})])
    with pytest.raises(EventLogCorruptError, match="PoolScored"):
        tr.typed(tr.PoolScored, {"points": "nope"})


def test_series_version_is_checked() -> None:
    with pytest.raises(EventLogCorruptError, match="series event 0"):
        sr.fold("s", [_created(sr.SERIES_CREATED, {}, v=sr.SERIES_SCHEMA_VERSION + 1)])
    with pytest.raises(EventLogCorruptError, match="SeriesState"):
        sr.fold("s", [_created(sr.SERIES_CREATED, {"junk": 1})])
