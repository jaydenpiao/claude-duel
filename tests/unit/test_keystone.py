"""Sealed Ledger step 3a: keystone commit-reveal and scoring (design-core-logic.md 4.2 step 1/6, 4.3, test 4)."""

from __future__ import annotations

import dataclasses

import pytest

from claude_duel.domain import events as ev
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import Attack, Challenge, ScoringRule, Validation
from claude_duel.domain.scoring import SCORERS, RoundFacts, defense_award, solve_award
from claude_duel.domain.stakes import keystone_commit, keystone_salt, open_keystone, pick_keystone
from tests.unit.test_domain import spec

RULE = ScoringRule()


def _facts(solved: bool, valid: bool = True, keystone: bool = True, probed: bool = False) -> RoundFacts:
    ch = Challenge(id="c0", round=1, author="a", spec=spec(difficulty=2), probe_solved=probed)
    atk = Attack(challenge_id="c0", attacker="b", solved=solved, attempts=1, tokens_used=0, reason="r", stake=4000)
    v = Validation(challenge_id="c0", valid=valid, reason="r")
    return RoundFacts(1, ("a", "b"), (ch,), {"c0": v}, {"c0": atk}, {"a": "c0"} if keystone else {})


def test_defended_keystone_doubles_defense() -> None:
    plain = SCORERS["ledger"](_facts(False, keystone=False), RULE)
    stone = SCORERS["ledger"](_facts(False), RULE)
    assert stone["a"] - plain["a"] == defense_award(spec(difficulty=2), 1, RULE)
    assert stone["b"] == plain["b"]


def test_probe_solved_keystone_doubles_its_halved_defense() -> None:
    plain = SCORERS["ledger"](_facts(False, keystone=False, probed=True), RULE)
    stone = SCORERS["ledger"](_facts(False, probed=True), RULE)
    half = defense_award(spec(difficulty=2), 1, RULE) // 2
    assert plain["a"] == half
    assert stone["a"] == 2 * half


def test_captured_keystone_costs_half_base_and_never_raises_the_capture() -> None:
    plain = SCORERS["ledger"](_facts(True, keystone=False), RULE)
    stone = SCORERS["ledger"](_facts(True), RULE)
    assert plain["a"] - stone["a"] == solve_award(spec(difficulty=2), 1, 1, RULE) // 2
    assert stone["b"] == plain["b"]


def test_invalid_keystone_forfeits() -> None:
    f = _facts(False, valid=False)
    assert SCORERS["ledger"](f, RULE) == SCORERS["ledger"](dataclasses.replace(f, keystones={}), RULE)


def test_commit_binds_index_and_salt() -> None:
    salt = keystone_salt("seed", 1, "alice")
    assert salt == keystone_salt("seed", 1, "alice") != keystone_salt("seed", 2, "alice")
    assert keystone_commit(1, salt) != keystone_commit(2, salt)
    assert keystone_commit(None, salt) != keystone_commit(0, salt)


@pytest.mark.parametrize(("raw", "want"), [(0, 0), (1, None), (2, None), (-1, None), (True, None), ("0", None)])
def test_pick_keystone_forfeits_bad_picks(raw: object, want: int | None) -> None:
    own = [Challenge(id=f"c{i}", round=1, author="a", spec=spec()) for i in range(2)]
    assert pick_keystone(raw, own, {"c0": True, "c1": False}) == want


def test_reveal_that_does_not_match_commit_is_tampered(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state()
    salt = keystone_salt("", 1, "a")
    ev._sealed(state, ev.KeystoneSealed(agent="a", round=1, commit=keystone_commit(0, salt)))
    with pytest.raises(EventLogCorruptError, match="tampered"):
        ev._revealed(state, ev.KeystoneRevealed(agent="a", round=1, index=1, salt=salt))
    ev._revealed(state, ev.KeystoneRevealed(agent="a", round=1, index=0, salt=salt))
    assert state.keystones["1:a"].revealed


def test_open_keystone_finds_the_sealed_index() -> None:
    salt = keystone_salt("s", 1, "a")
    assert open_keystone(keystone_commit(2, salt), salt, 3) == 2
    assert open_keystone(keystone_commit(None, salt), salt, 3) is None
    with pytest.raises(ValueError, match="no index"):
        open_keystone("0" * 64, salt, 3)
