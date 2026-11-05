"""Heat lines in a real (fake-model) ledger match: logged, replayed, disclosed (design-core-logic.md test 9)."""

from __future__ import annotations

import platform

from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain import memory
from claude_duel.domain.models import MatchState
from claude_duel.domain.modes import preset


def test_heat_lines_are_logged_replayed_and_shown_in_memory(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state: MatchState = new_state("h1")
    rules = preset("ledger")
    assert rules.ledger is not None
    state.rules = rules.model_copy(
        update={"rounds": 3, "ledger": rules.ledger.model_copy(update={"heat_threshold_pct": 1})}
    )
    state = make_engine().run(state)
    events = event_log.read("h1")
    assert events[0].data["python"] == platform.python_version()
    scored = [e for e in events if e.type == ev.ROUND_SCORED]
    lines = {cid: pts for e in scored for cid, pts in e.data.get("heat", {}).items()}
    assert lines, "a 1% threshold heats some round-2/3 challenge"
    assert replay(state, events).consistent
    again = [e for e in event_log.read("h1") if e.type == ev.ROUND_SCORED]
    assert [e.data for e in again] == [e.data for e in scored]
    author = state.challenges[next(iter(lines))].author
    assert "(heat " in memory.summarize(state, author, state.rules.rounds + 1, state.rules.memory)
    forged = [
        e.model_copy(update={"data": {**e.data, "heat": {k: v + 1 for k, v in e.data["heat"].items()}}})
        if e.type == ev.ROUND_SCORED and e.data.get("heat")
        else e
        for e in events
    ]
    assert any(m.endswith(" heat tampered") for m in replay(state, forged).mismatches)
