"""Puzzle variety in the check form (#79), with fake models."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.domain import variety
from tests.unit.test_contract import Builds

VARIED = {"rounds": 2, "challenges_per_round": 1, "challenge_form": "check", "variety": True}
A, B, C = agents.CHECK_EXAMPLES


def test_themes_are_seeded_and_distinct_per_agent() -> None:
    first = [variety.theme("s1", "alice", r, i, 2) for r in (1, 2, 3, 4) for i in (0, 1)]
    assert len(set(first)) == len(variety.THEMES)
    assert first == [variety.theme("s1", "alice", r, i, 2) for r in (1, 2, 3, 4) for i in (0, 1)]
    assert first != [variety.theme("s2", "alice", r, i, 2) for r in (1, 2, 3, 4) for i in (0, 1)]


def test_repeat_is_rejected_and_past_designs_are_shown(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Builds(A, A, B)  # round 1: A; round 2: A again (rejected as a repeat), then B
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**VARIED))
    round2 = next(c for c in state.challenges.values() if c.author == "bob" and c.round == 2)
    assert state.build_errors[round2.id][0].startswith("repeat design")
    assert round2.spec is not None
    assert round2.spec.title == B["title"]
    assert all("THEME for this challenge" in p for p in bob.prompts)
    assert "Your past designs" not in bob.prompts[0]
    assert f"{A['title']!r} [" in bob.prompts[1]


def test_same_source_under_a_new_title_is_a_repeat(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Builds(A, {**A, "title": "Renamed"}, C)
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**VARIED))
    round2 = next(c for c in state.challenges.values() if c.author == "bob" and c.round == 2)
    assert "same as an earlier one" in state.build_errors[round2.id][0]


def test_off_by_default(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Builds(A)
    make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**{**VARIED, "variety": False}))
    assert not any("THEME" in p for p in bob.prompts)
