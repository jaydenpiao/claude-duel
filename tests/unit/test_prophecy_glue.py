"""Prophecy domain glue (design-prophecy-wiring.md P1): rule, salt, open, payloads, artifacts, cap. No engine."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from claude_duel.app.engine import MatchEngine
from claude_duel.domain import events as ev
from claude_duel.domain import prophecy
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import (
    Attack,
    Challenge,
    DeepRule,
    Keystone,
    MatchState,
    ProphecyRule,
    Rules,
    Validation,
)
from claude_duel.domain.modes import preset
from claude_duel.domain.prophecy_state import artifacts_of, round_cap
from claude_duel.domain.stakes import keystone_commit, keystone_salt
from tests.unit.test_domain import spec

NewState = Callable[..., MatchState]
MakeEngine = Callable[..., MatchEngine]


def test_prophecy_is_off_by_default_and_validates() -> None:
    assert DeepRule().prophecy is None
    assert preset("ledger_deep").deep is not None
    assert preset("ledger_deep").deep.prophecy is None
    rule = ProphecyRule()
    assert (rule.max_per_round, rule.pool, rule.round_cap, rule.rates_version) == (2, 6, 3, 0)
    with pytest.raises(ValueError):
        ProphecyRule(max_per_round=3)
    assert prophecy.PROPHECY_RATES[0] == {}


def test_all_in_tracks_attack_tokens() -> None:
    assert prophecy.TEMPLATES["all_in"][0].n == 6_000
    assert prophecy.templates(9_000)["all_in"][0].n == 4_500
    got = prophecy.pick('{"prophecies": [{"template": "all_in", "stake": 1}]}', attack_tokens=4_000)
    assert got is not None
    assert got[0].atoms[0].n == 2_000


def test_salt_is_domain_separated_from_the_keystone() -> None:
    salt = prophecy.prophecy_salt("seed", 1, "a", 0)
    assert salt == prophecy.prophecy_salt("seed", 1, "a", 0)
    assert salt != keystone_salt("seed", 1, "a")
    assert len({prophecy.prophecy_salt("seed", r, a, s) for r in (1, 2) for a in "ab" for s in (0, 1)}) == 8


@pytest.mark.parametrize("name", sorted(prophecy.TEMPLATES))
@pytest.mark.parametrize("stake", [1, 2, 3])
def test_every_menu_pick_opens_uniquely(name: str, stake: int) -> None:
    salt = prophecy.prophecy_salt("s", 1, "a", 0)
    p = prophecy.make(name, stake)
    assert prophecy.open_prophecy(prophecy.commit(p, salt), salt) == p
    with pytest.raises(ValueError, match="no menu pick"):
        prophecy.open_prophecy(prophecy.commit(p, salt), salt + "x")


def test_round_cap_never_exceeds_the_cheapest_defense_nor_drops_below_one() -> None:
    assert prophecy.round_cap(3, [10, 20]) == 3
    assert prophecy.round_cap(3, [2, 20]) == 2
    assert prophecy.round_cap(3, [0]) == 1
    assert prophecy.round_cap(3, []) == 3


def _state(new_state) -> MatchState:
    state: MatchState = new_state()
    state.rules = preset("ledger_deep").model_copy(update={"rounds": 3})
    return state


def _seal(state: MatchState, p: prophecy.Prophecy, salt: str) -> None:
    ev._prophecy_sealed(
        state, ev.ProphecySealed(agent="a", round=1, target_round=2, slot=0, commit=prophecy.commit(p, salt))
    )


def test_seal_reveal_settle_round_trip(new_state: NewState) -> None:
    state, salt, p = _state(new_state), prophecy.prophecy_salt("", 1, "a", 0), prophecy.make("uses_vm", 2)
    _seal(state, p, salt)
    assert not state.prophecies["2:a:0"].revealed
    ev._prophecy_revealed(
        state, ev.ProphecyRevealed(agent="a", round=2, slot=0, template="uses_vm", stake=2, salt=salt)
    )
    seal = state.prophecies["2:a:0"]
    assert (seal.template, seal.stake, seal.revealed) == ("uses_vm", 2, True)
    ev._prophecy_settled(state, ev.ProphecySettled(agent="a", round=2, hits=[True], vacuous=[False], delta=2))
    ev._prophecy_settled(state, ev.ProphecySettled(agent="a", round=3, hits=[False], vacuous=[False], delta=-1))
    assert (state.prophecy_points["a"], state.prophecy_swing["a"]) == (1, 3)
    ev._prophecy_skipped(state, ev.ProphecySkipped(agent="b", round=1, reason="malformed"))


@pytest.mark.parametrize(
    "bad",
    [
        {"stake": 3},
        {"template": "uses_maze"},
        {"template": "no_such"},
        {"salt": "wrong"},
        {"slot": 1},
    ],
)
def test_tampered_reveal_is_rejected(new_state: NewState, bad: dict[str, object]) -> None:
    state, salt = _state(new_state), prophecy.prophecy_salt("", 1, "a", 0)
    _seal(state, prophecy.make("uses_vm", 2), salt)
    data = {"agent": "a", "round": 2, "slot": 0, "template": "uses_vm", "stake": 2, "salt": salt} | bad
    with pytest.raises(EventLogCorruptError, match="tampered"):
        ev._prophecy_revealed(state, ev.ProphecyRevealed.model_validate(data))


def test_seal_must_target_the_next_round(new_state: NewState) -> None:
    with pytest.raises(EventLogCorruptError, match="targets"):
        ev._prophecy_sealed(
            _state(new_state), ev.ProphecySealed(agent="a", round=1, target_round=3, slot=0, commit="0")
        )


def test_payloads_forbid_extras_and_bad_reasons() -> None:
    with pytest.raises(ValueError):
        ev.ProphecySkipped.model_validate({"agent": "a", "round": 1, "reason": "bored"})
    with pytest.raises(ValueError):
        ev.ProphecySealed.model_validate({"agent": "a", "round": 1, "target_round": 2, "slot": 2, "commit": "0"})
    assert ev.RoundScored(round=1, delta={}).prophecy == {}


def _round2(state: MatchState) -> None:
    """b authors c1 (valid, vm, keystone), c2 (valid, ghost-killed), c3 (invalid); b captures one of a's live."""
    vm = "stack = []\nprint(stack)\n"
    for cid, author, valid in (
        ("c1", "b", True),
        ("c2", "b", True),
        ("c3", "b", False),
        ("c4", "a", True),
        ("c5", "a", True),
    ):
        state.challenges[cid] = Challenge(
            id=cid, round=2, author=author, spec=spec(source=vm if cid == "c1" else "print(1)\n")
        )
        state.validations[cid] = Validation(challenge_id=cid, valid=valid, reason="r")
    state.challenges["c6"] = Challenge(id="c6", round=2, author="b", spec=None, forfeit_reason="x")
    atk = {"challenge_id": "c4", "attacker": "b", "solved": True, "attempts": 1, "tokens_used": 1, "reason": "r"}
    state.attacks["c4:b"] = Attack.model_validate(atk)
    state.attacks["c5:b"] = Attack.model_validate(atk | {"challenge_id": "c5", "ghost": "c9:b"})
    state.ghost_runs["2:c9:a>c2"] = True
    state.stakes["2:b"] = [7_000, 5_000]
    salt = keystone_salt("", 2, "b")
    state.keystones["2:b"] = Keystone(commit=keystone_commit(0, salt), index=0, salt=salt, revealed=True)


def test_artifacts_come_from_the_log(new_state: NewState) -> None:
    state = _state(new_state)
    _round2(state)
    art = artifacts_of(state, "b", 2)
    assert art is not None
    assert [c.cid for c in art.challenges] == ["c1", "c2"]  # invalid c3 and forfeited c6 excluded
    assert art.challenges[0].tag == "vm"
    assert art.keystone == "c1"
    assert (art.captures, art.ghost_kills, art.stakes) == (1, 1, (7_000, 5_000))  # ghost capture is not live
    assert prophecy.holds(prophecy.make("all_in", 1), art)
    assert prophecy.holds(prophecy.make("keystone_first", 1), art)


def test_target_with_no_valid_challenge_is_void(new_state: NewState) -> None:
    state = _state(new_state)
    assert artifacts_of(state, "b", 2) is None
    _round2(state)
    state.stakes.pop("2:b")
    art = artifacts_of(state, "b", 2)
    assert art is not None
    assert art.stakes == ()


def test_round_one_defense_awards_do_not_mute_the_cap(make_engine: MakeEngine, new_state: NewState) -> None:
    """Open question 5, on a real fake-provider ledger_deep match: round 1's cheapest defense award is >= 3."""
    state = new_state("p1")
    state.rules = preset("ledger_deep").model_copy(
        update={"rounds": 2, "deep": DeepRule(ghosts=True, prophecy=ProphecyRule())}
    )
    state.seed = "seed-1"
    done = make_engine().run(state)
    for agent in ("alice", "bob"):
        if artifacts_of(done, agent, 1) is not None:
            assert round_cap(done, agent, 1) == ProphecyRule().round_cap
    assert any(artifacts_of(done, a, 1) is not None for a in ("alice", "bob"))


def test_round_cap_without_a_prophecy_rule_uses_the_default(new_state: NewState) -> None:
    state = _state(new_state)
    _round2(state)
    state.rules = Rules(rounds=3)
    assert round_cap(state, "b", 2) == 3
    state.rules = preset("ledger_deep").model_copy(update={"deep": DeepRule(prophecy=ProphecyRule(round_cap=1))})
    assert round_cap(state, "b", 2) == 1
