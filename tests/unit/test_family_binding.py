"""R2 follow-ups: declared-family binding, cross-match family bans, and the per-axis example pool."""

from __future__ import annotations

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.domain import events, variety
from claude_duel.domain.models import AgentSpec, ChallengeSpec, MatchState, Rules, SandboxLimits
from claude_duel.domain.modes import preset
from tests.unit.test_contract import Builds

AGENTS = ["haiku", "mini"]


def _checked(**over: object) -> dict[str, object]:
    return {**preset("checked").model_dump(), "rounds": 1, "challenges_per_round": 1, **over}


@pytest.mark.parametrize("axis", list(variety.AXIS_EXAMPLES))
def test_every_axis_example_passes_the_checked_arena(make_engine, new_state, axis) -> None:  # type: ignore[no-untyped-def]
    example = {**variety.AXIS_EXAMPLES[axis], "family": ""}  # the slot's draw differs; binding tested below
    state = make_engine({"alice": FakeProvider(), "bob": Builds(example)}).run(new_state(**_checked()))
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[mine.id].valid, state.build_errors
    assert (state.validations[mine.id].solver_checks or 0) <= 2_000


def test_family_ids_are_unique_and_examples_declare_real_ones() -> None:
    ids = [variety.family_id(f) for fams in variety.TAXONOMY.values() for f in fams]
    assert len(ids) == len(set(ids))
    for axis, ex in variety.AXIS_EXAMPLES.items():
        assert ex["family"] in {variety.family_id(f) for f in variety.TAXONOMY[axis]}


def _state(seed: str = "s", bans: dict[str, list[str]] | None = None, model: str = "m") -> MatchState:
    return MatchState(
        match_id=seed,
        seed=seed,
        agents=(AgentSpec(name="haiku", model=model), AgentSpec(name="mini", model="n")),
        rules=Rules(**_checked(rounds=3, challenges_per_round=2)),  # type: ignore[arg-type]
        limits=SandboxLimits(),
        family_bans=bans or {},
    )


def _spec(family: str) -> ChallengeSpec:
    return ChallengeSpec(
        title="t",
        description="d",
        source="def check(s):\n    return 0\n",
        reference_payload="",
        difficulty=1,
        family=family,
    )


def test_declared_family_must_match_the_draw() -> None:
    state = _state()
    state.round = 1
    _, fam = variety.state_family(state, "haiku", 0)
    assert variety.family_reason(state, "haiku", 0, _spec(variety.family_id(fam))) is None
    assert variety.family_reason(state, "haiku", 0, _spec("")) is None  # tolerated: older replies
    reason = variety.family_reason(state, "haiku", 0, _spec("roman-numeral-arithmetic-x"))
    assert reason is not None
    assert variety.family_id(fam) in reason


def test_bans_steer_the_draw_and_keep_the_guarantees() -> None:
    for n in range(300):
        seed = f"s{n}"
        first = variety.drawn(_state(seed))
        bans = {"haiku": first["haiku"]}
        banned = variety.drawn(_state(seed, bans))
        assert len(set(banned["haiku"])) == len(banned["haiku"])  # still no repeats
        assert len(set(banned["haiku"]) & set(first["haiku"])) < len(first["haiku"])
        for a, b in zip(banned["haiku"], banned["mini"], strict=True):
            assert a != b


def test_history_bans_use_the_same_model_last_three_matches() -> None:
    past = [_state(f"p{i}", model="m") for i in range(5)]
    for i, m in enumerate(past):
        m.created_seq = i
    other = _state("q", model="zzz")
    bans = variety.history_bans([*past, other], (AgentSpec(name="haiku", model="m"),))
    want = sorted({f for m in past[-3:] for f in variety.drawn(m)["haiku"]})
    assert bans == {"haiku": want}
    assert variety.history_bans([other], (AgentSpec(name="haiku", model="m"),)) == {}


def test_bans_are_frozen_into_match_created() -> None:
    state = _state(bans={"haiku": ["x"]})
    data = events.created_data(state)
    assert data["family_bans"] == {"haiku": ["x"]}
    assert "family_bans" not in events.created_data(_state())  # old logs stay byte-identical
