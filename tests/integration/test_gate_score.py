"""Difficulty gate in gate_mode "score" (#78): a probe-solved challenge stays valid at half defense value."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.domain.models import Challenge, ChallengeSpec, Rules, Validation
from claude_duel.domain.scoring import defense_award, score_round
from tests.integration.test_build_repair import Probe, Scripted

ONE = {"rounds": 1, "challenges_per_round": 1, "difficulty_gate": True}


def _mine(state, author):  # type: ignore[no-untyped-def]
    return next(c for c in state.challenges.values() if c.author == author)


def test_probe_solve_is_kept_and_marked(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Scripted(json.dumps({**agents.EXAMPLE_CHALLENGE, "probe_solved": False}))
    engine = make_engine({"alice": alice, "bob": FakeProvider(), "gate": Probe()})
    state = engine.run(new_state(**ONE, gate_mode="score"))
    mine = _mine(state, "alice")
    assert len(alice.prompts) == 1  # no repair round-trip
    assert state.validations[mine.id].valid
    assert mine.spec is not None
    assert mine.spec.probe_solved


def test_author_cannot_mark_itself(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Scripted(json.dumps({**agents.EXAMPLE_CHALLENGE, "probe_solved": True}))
    state = make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    spec = _mine(state, "alice").spec
    assert spec is not None
    assert not spec.probe_solved


def test_reject_mode_is_unchanged(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Scripted(json.dumps(agents.EXAMPLE_CHALLENGE), json.dumps(agents.CHECK_EXAMPLES[1]))
    engine = make_engine({"alice": alice, "bob": FakeProvider(), "gate": Probe()})
    state = engine.run(new_state(**ONE))
    assert state.build_errors[_mine(state, "alice").id][0].startswith("too easy")


def test_probe_solved_defense_pays_half() -> None:
    rules = Rules()
    spec = ChallengeSpec.model_validate(agents.EXAMPLE_CHALLENGE)
    full = defense_award(spec, 1, rules.scoring)
    for marked, expected in ((False, full), (True, full // 2)):
        ch = Challenge(id="c", round=1, author="a", spec=spec.model_copy(update={"probe_solved": marked}))
        delta = score_round(
            1, ["a", "b"], [ch], {"c": Validation(challenge_id="c", valid=True, reason="ok")}, {}, rules
        )
        assert delta["a"] == expected
