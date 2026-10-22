"""Per-agent handicaps (#38): limits per agent, pinned in match_created, shown in commentary."""

from __future__ import annotations

import io

from claude_duel.app import allowance
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.models import Handicap, MatchState
from claude_duel.presentation.commentary import Commentary
from claude_duel.presentation.common import handicap_text, result

HANDICAPPED = Handicap(attempts_delta=-3, token_budget_mult=0.5, bank_mult=2.0, score_offset=7)


def _handicapped(state: MatchState) -> MatchState:
    a, b = state.agents
    return state.model_copy(update={"agents": (a.model_copy(update={"handicap": HANDICAPPED}), b)})


def test_limits_are_read_per_agent(new_state) -> None:  # type: ignore[no-untyped-def]
    state = _handicapped(new_state(max_attempts=4, attack_token_budget=1000, clock={"bank_tokens": 100}))
    a, b = (x.name for x in state.agents)
    assert (state.max_attempts(a), state.max_attempts(b)) == (1, 4)
    assert (state.attack_token_budget(a), state.attack_token_budget(b)) == (500, 1000)
    assert (allowance.clock_left(state, a), allowance.clock_left(state, b)) == (200, 100)
    assert state.max_attempts("nobody") == 4


def test_handicapped_match_runs_replays_and_records(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_handicapped(new_state()))
    a = state.agents[0].name
    assert all(x.attempts <= 1 for x in state.attacks.values() if x.attacker == a)
    assert replay(state, event_log.read(state.match_id)).consistent
    created = event_log.read(state.match_id)[0]
    assert ev.initial_state(state.match_id, created).agents[0].handicap == HANDICAPPED
    assert state.final_scores()[a] == state.scores.get(a, 0) + 7


def test_offset_decides_the_result(new_state) -> None:  # type: ignore[no-untyped-def]
    state = _handicapped(new_state())
    a, b = (x.name for x in state.agents)
    state.scores = {a: 0, b: 5}
    assert result(state).startswith(f"winner: {a}")
    state.scores = {a: 0, b: 7}
    assert result(state).startswith("draw")


def test_commentary_shows_handicaps(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    out = io.StringIO()
    make_engine(observer=Commentary(out, "m1", 50.0, lambda: 0.0)).run(_handicapped(new_state()))
    text = out.getvalue()
    assert "handicap alice: tries -3, attack tokens x0.5, clock x2, score +7" in text
    assert handicap_text(Handicap()) == ""
    assert not Handicap().is_set()
