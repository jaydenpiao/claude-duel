"""Sealed Ledger step 2: stakes, the ledger scorer and its tiebreak (design-core-logic.md tests 5-8)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import (
    AgentSpec,
    Attack,
    Challenge,
    MatchState,
    Rules,
    SandboxLimits,
    ScoringRule,
    Usage,
    Validation,
)
from claude_duel.domain.ratings import raw_winner
from claude_duel.domain.scoring import SCORERS, RoundFacts, classic, solve_award, stake_award
from claude_duel.domain.series import game_winner
from claude_duel.domain.stakes import LedgerRule, even_split, settle_stakes
from claude_duel.presentation.common import leader, result
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
    assert SCORERS["ledger"](_facts(0, 0, solved=False), ScoringRule()) == held


@pytest.mark.parametrize(("stake", "match"), [(0, "tampered"), (None, "no stake")])
def test_capture_without_a_live_stake_is_corrupt(stake: int | None, match: str) -> None:
    with pytest.raises(EventLogCorruptError, match=match):
        SCORERS["ledger"](_facts(stake, 0), ScoringRule())


@given(st.integers(0, 40), st.integers(1, 50_000), st.integers(1, 5_000))
def test_fallback_always_passes_its_own_check(k: int, tokens: int, min_alloc: int) -> None:
    rule = LedgerRule(attack_tokens=tokens, min_alloc=min_alloc)
    split = even_split(k, rule)
    assert settle_stakes(split, k, rule) == (split, False)


def test_fallback_when_even_share_is_below_min_alloc() -> None:
    assert even_split(13, RULE) == (1000,) * 12 + (0,)
    assert even_split(0, RULE) == ()


def _tied(alice: Usage, bob: Usage) -> MatchState:
    state = MatchState(
        match_id="t", agents=(AgentSpec(name="alice"), AgentSpec(name="bob")),
        rules=Rules(scoring=ScoringRule(kind="ledger"), ledger=RULE), limits=SandboxLimits(),
        scores={"alice": 10, "bob": 10}, usage_by_agent={"alice": alice, "bob": bob},
    )  # fmt: skip
    return state


def test_tie_goes_to_fewer_dollars_then_fewer_tokens() -> None:
    assert game_winner(_tied(Usage(usd=0.002, tokens=900), Usage(usd=0.001, tokens=5000))) == "bob"
    assert game_winner(_tied(Usage(usd=0.001, tokens=900), Usage(usd=0.001, tokens=5000))) == "alice"
    assert game_winner(_tied(Usage(usd=0.001, tokens=900), Usage(usd=0.001, tokens=900))) is None


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


def test_one_tiebreak_for_series_ratings_and_live_result() -> None:
    s = _tied(Usage(usd=0.002, tokens=900), Usage(usd=0.001, tokens=5000))
    assert game_winner(s) == raw_winner(s) == leader(s) == "bob"
    assert result(s).startswith("winner: bob")
