from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.app.engine import MatchEngine
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.errors import EventLogCorruptError, MatchNotFoundError
from claude_duel.domain.models import Event, MatchState


def _event(seq: int, type_: str = ev.STAGE_CHANGED, **data: object) -> Event:
    return Event(seq=seq, type=type_, trace_id="t", data=data)


def _played(make_engine: Callable[..., MatchEngine], new_state: Callable[..., MatchState]) -> MatchState:
    return make_engine().run(new_state())


def test_round_trip_and_missing_log(event_log: FileEventLog) -> None:
    assert event_log.read("m1") == []
    event_log.append("m1", _event(0, ev.MATCH_CREATED))
    event_log.append("m1", _event(1, to="build"))
    assert [e.seq for e in event_log.read("m1")] == [0, 1]


def test_out_of_order_append_rejected(event_log: FileEventLog) -> None:
    event_log.append("m1", _event(0))
    with pytest.raises(EventLogCorruptError, match="expected 1"):
        event_log.append("m1", _event(5))


def test_invalid_match_id(event_log: FileEventLog) -> None:
    with pytest.raises(MatchNotFoundError):
        event_log.read("../x")


@pytest.mark.parametrize("mutate", ["edit", "drop", "garbage"])
def test_tampering_is_detected(event_log: FileEventLog, store_root: Path, mutate: str) -> None:
    for seq in range(3):
        event_log.append("m1", _event(seq, to="build"))
    path = store_root / "m1.events.jsonl"
    lines = path.read_text().splitlines()
    if mutate == "edit":
        rec = json.loads(lines[1])
        rec["event"]["data"]["to"] = "complete"
        lines[1] = json.dumps(rec)
    elif mutate == "drop":
        del lines[1]
    else:
        lines[1] = "{not json"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(EventLogCorruptError):
        event_log.read("m1")


def test_fold_rejects_bad_logs() -> None:
    with pytest.raises(EventLogCorruptError, match="empty"):
        ev.fold("m1", [])
    with pytest.raises(EventLogCorruptError, match="match_created"):
        ev.fold("m1", [_event(0)])


def test_apply_rejects_gaps_and_unknown_types(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state = _played(make_engine, new_state)
    with pytest.raises(EventLogCorruptError, match="applied after"):
        ev.apply(state, _event(state.last_seq + 2))
    with pytest.raises(EventLogCorruptError, match="unknown event type"):
        ev.apply(state, _event(state.last_seq + 1, "nope"))


def test_created_event_never_holds_secret(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = _played(make_engine, new_state)
    assert state.secret not in json.dumps([e.model_dump(mode="json") for e in event_log.read("m1")])


def test_replay_flags_snapshot_drift(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = _played(make_engine, new_state)
    events = event_log.read("m1")
    state.scores = {"alice": 9999, "bob": 0}
    report = replay(state, events)
    assert not report.consistent
    assert "snapshot.scores" in report.mismatches


def test_replay_flags_rescored_round(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = _played(make_engine, new_state)
    events = [
        e.model_copy(update={"data": {**e.data, "delta": {"alice": 1, "bob": 1}}}) if e.type == ev.ROUND_SCORED else e
        for e in event_log.read("m1")
    ]
    report = replay(state, events)
    assert "round 1 score" in report.mismatches
    assert "total score" in report.mismatches


def test_replay_ignores_events_past_snapshot(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = _played(make_engine, new_state)
    events = [*event_log.read("m1"), _event(state.last_seq + 1, to="build")]
    assert replay(state, events).consistent
