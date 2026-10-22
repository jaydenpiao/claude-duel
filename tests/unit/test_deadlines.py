"""Match deadline (#13): measured on recorded provider time, so a resume cannot reset it."""

from __future__ import annotations

import itertools

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import engine as engine_mod
from claude_duel.domain import events as ev
from claude_duel.domain.errors import DeadlineExceededError
from claude_duel.domain.models import Stage


def test_match_fails_with_deadline_and_resume_keeps_the_total(  # type: ignore[no-untyped-def]
    make_engine, new_state, event_log, monkeypatch
) -> None:
    ticks = itertools.count(0, 0.5)  # every clock read advances 0.5s, so each call records 500 ms
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: next(ticks))
    providers = {"alice": FakeProvider(skill=2), "bob": FakeProvider(skill=3)}
    state = new_state(rounds=3, match_deadline_s=2)
    with pytest.raises(DeadlineExceededError, match="deadline"):
        make_engine(providers).run(state)
    assert state.stage is Stage.FAILED
    spent = state.usage.elapsed_ms
    assert spent >= 2000
    folded = ev.fold(state.match_id, event_log.read(state.match_id))
    assert folded.usage.elapsed_ms == spent
    assert "deadline" in event_log.read(state.match_id)[-1].data["reason"]
    with pytest.raises(DeadlineExceededError):
        make_engine(providers).run(state)  # resume: same total, fails again before any new call
    assert state.usage.elapsed_ms == spent


def test_default_deadline_is_900s(new_state) -> None:  # type: ignore[no-untyped-def]
    assert new_state().rules.match_deadline_s == 900
