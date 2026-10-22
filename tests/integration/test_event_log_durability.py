"""Crash-injection tests for event-log durability (architecture round 2, N1-N3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.errors import InvalidTransitionError
from claude_duel.domain.models import Event, Stage


def _event(seq: int) -> Event:
    return Event(seq=seq, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "build"})


def test_torn_tail_is_dropped_before_next_append(event_log, store_root: Path) -> None:  # type: ignore[no-untyped-def]
    event_log.append("m1", _event(0))
    event_log.append("m1", _event(1))
    path = store_root / "m1.events.jsonl"
    with path.open("ab") as fh:
        fh.write(b'{"prev": "abc", "hash": "de')  # crash mid-write: never acknowledged
    assert [e.seq for e in event_log.read("m1")] == [0, 1]  # readers ignore the torn tail
    event_log.append("m1", _event(2))
    assert [e.seq for e in event_log.read("m1")] == [0, 1, 2]
    assert path.read_bytes().endswith(b"}\n")
    assert b'"hash": "de' not in path.read_bytes()


def test_crash_between_first_event_and_first_snapshot(make_engine, new_state, store, event_log) -> None:  # type: ignore[no-untyped-def]
    real_save = store.save

    def crash_on_save(state):  # type: ignore[no-untyped-def]
        raise RuntimeError("power cut")

    state = new_state()
    real_save(state)  # the session's pre-run snapshot, last_seq == -1
    store.save = crash_on_save
    with pytest.raises(RuntimeError):
        make_engine().run(state)
    store.save = real_save
    stale = store.load("m1")
    assert stale.last_seq == -1
    assert [e.type for e in event_log.read("m1")] == [ev.MATCH_CREATED]
    done = make_engine().run(stale)
    assert done.stage is Stage.COMPLETE
    assert replay(done, event_log.read("m1")).consistent


def test_unappliable_event_never_reaches_the_log(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine()
    state = new_state()
    engine._sync(state)
    before = event_log.read("m1")
    with pytest.raises(InvalidTransitionError):
        engine._checkpoint(state, ev.STAGE_CHANGED, to="complete")  # pending -> complete is illegal
    assert event_log.read("m1") == before
    assert state.last_seq == before[-1].seq
    assert engine.run(state).stage is Stage.COMPLETE  # still resumable
