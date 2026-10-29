"""Sealed Ledger step 2: stake checks and the ledger scorer (design-core-logic.md tests 5-8)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.domain.ledger import LedgerRule, even_split, settle_stakes
from claude_duel.domain.models import Attack, Challenge, ScoringRule, Validation
from claude_duel.domain.scoring import SCORERS, RoundFacts, classic, solve_award, stake_award
from tests.unit.test_domain import spec

RULE = LedgerRule()


@pytest.mark.parametrize("raw", [[6000, 4000, 0], [4000, 4000, 4000], [0, 0, 0], [1000, 0, 11000]])
def test_valid_stakes_kept(raw: list[int]) -> None:
    assert settle_stakes(raw, 3, RULE) == (tuple(raw), False)


@pytest.mark.parametrize(
    "raw",
    [
        [6000, 6000, 1],
        [-1, 0, 0],
        [500, 0, 0],
        [12000, 1000, 0],
        [4000, 4000],
        "abc",
        None,
        [True, 0, 0],
        [1.5e3, 0, 0],
    ],
)
def test_bad_stakes_fall_back_to_even_split(raw: object) -> None:
    assert settle_stakes(raw, 3, RULE) == ((4000, 4000, 4000), True)


def test_even_split_is_exact() -> None:
    assert even_split(5, LedgerRule(attack_tokens=12_002)) == (2401, 2401, 2400, 2400, 2400)


def _facts(stake: int | None, used: int, solved: bool = True) -> RoundFacts:
    ch = Challenge(id="c0", round=1, author="a", spec=spec(difficulty=2))
    atk = Attack(challenge_id="c0", attacker="b", solved=solved, attempts=1, tokens_used=used, reason="r", stake=stake)
    v = Validation(challenge_id="c0", valid=True, reason="r")
    return RoundFacts(1, ("a", "b"), (ch,), {"c0": v}, {"c0": atk})


def test_stake_zero_scores_as_defended() -> None:
    held = classic(_facts(None, 0, solved=False), ScoringRule())
    assert SCORERS["ledger"](_facts(0, 0), ScoringRule()) == held


def test_capture_discount_by_share_of_stake() -> None:
    full = solve_award(spec(difficulty=2), 1, 1, ScoringRule())  # 20
    assert SCORERS["ledger"](_facts(4000, 4000), ScoringRule())["b"] == full // 2
    assert SCORERS["ledger"](_facts(4000, 0), ScoringRule())["b"] == full
    assert SCORERS["ledger"](_facts(4000, 1000), ScoringRule())["b"] == full - full // 8


@given(st.integers(1, 10_000), st.integers(0, 50_000), st.integers(1, 20_000))
def test_stake_award_bounds(full: int, used: int, stake: int) -> None:
    assert 1 <= stake_award(full, used, stake) <= full
    assert stake_award(full, used, stake) >= full - full // 2


@given(st.integers(1, 3), st.integers(1, 20_000), st.integers(0, 40_000))
def test_ledger_defense_at_least_capture(difficulty: int, stake: int, used: int) -> None:
    ch = Challenge(id="c0", round=1, author="a", spec=spec(difficulty=difficulty))
    v = {"c0": Validation(challenge_id="c0", valid=True, reason="r")}

    def atk(solved: bool) -> dict[str, Attack]:
        a = Attack(
            challenge_id="c0", attacker="b", solved=solved, attempts=1, tokens_used=used, reason="r", stake=stake
        )
        return {"c0": a}

    held = SCORERS["ledger"](RoundFacts(1, ("a", "b"), (ch,), v, atk(False)), ScoringRule())["a"]
    cap = SCORERS["ledger"](RoundFacts(1, ("a", "b"), (ch,), v, atk(True)), ScoringRule())["b"]
    assert held >= cap
