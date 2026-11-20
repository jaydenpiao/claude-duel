"""Duplicate play core: board seeds, mirrored halves, clip, harness-fault voids, stats and the stop rule ($0)."""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from claude_duel.domain import duplicate as dp
from claude_duel.domain import match_stats as ms
from claude_duel.domain import modes
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import (
    TRUNCATED,
    AgentSpec,
    AttackAttempt,
    Event,
    MatchState,
    Rules,
    SandboxLimits,
    Usage,
    Validation,
)

A, B = AgentSpec(name="gpt-x"), AgentSpec(name="claude-y")
RULE = dp.DuplicateRule()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _half(match_id: str, agents: tuple[AgentSpec, AgentSpec], scores: dict[str, int], **kw: object) -> MatchState:
    return MatchState(
        match_id=match_id,
        agents=agents,
        rules=Rules(),
        limits=SandboxLimits(),
        scores=scores,
        **kw,  # type: ignore[arg-type]
    )


def _state(diffs: tuple[int, ...] | list[int] = (), rule: dp.DuplicateRule = RULE) -> dp.DuplicateState:
    st = dp.DuplicateState(duplicate_id="d", seed="s", rule=rule, agents=(A, B), rules=Rules(), limits=SandboxLimits())
    for i, d in enumerate(diffs, 1):
        st.boards.append(dp.Board(board_no=i, seed="x", halves=("h1", "h2"), raw=d, diff=d))
    return st


def test_seeds_and_halves_are_deterministic_and_mirrored() -> None:
    assert dp.board_seed("s", 1) == dp.board_seed("s", 1) != dp.board_seed("s", 2)
    assert dp.replay_seed("b", 0) == "b" != dp.replay_seed("b", 1) != dp.replay_seed("b", 2)
    assert dp.half_agents((A, B), 0) == (A, B)
    assert dp.half_agents((A, B), 1) == (B, A)
    assert _state().half_id(3, 1, 2) == "d-b3b-r2"


def test_identical_play_in_both_seats_scores_zero_and_big_swings_clip() -> None:
    st = _state()
    seat_edge = (_half("h1", (A, B), {"gpt-x": 30, "claude-y": 0}), _half("h2", (B, A), {"claude-y": 30, "gpt-x": 0}))
    assert dp.board_of(st, 1, "x", seat_edge, 0).diff == 0  # first-seat advantage cancels across the board
    cap = dp.board_cap(RULE, Rules())
    assert cap == 2 * Rules().scoring.defense_points * 3 * Rules().rounds  # linear: the last round weighs most
    flat = Rules().model_copy(update={"scoring": Rules().scoring.model_copy(update={"round_weight": "flat"})})
    assert dp.board_cap(RULE, flat) == 2 * Rules().scoring.defense_points * 3
    assert dp.board_cap(RULE, modes.preset("ladder")) == 2 * Rules().scoring.defense_points * 3
    blowout = (_half("h1", (A, B), {"gpt-x": 500}), _half("h2", (B, A), {"gpt-x": 400}))
    board = dp.board_of(st, 1, "x", blowout, 0)
    assert (board.raw, board.diff) == (900, cap)


def test_harness_faults_void_and_model_failures_score() -> None:
    truncated = Validation(challenge_id="c", valid=False, reason=f"malformed: {TRUNCATED}")
    near_miss = Validation(challenge_id="c", valid=False, reason="reference payload does not reveal flag")
    junk = Validation(challenge_id="c", valid=False, reason="malformed: no JSON object found")
    assert dp.fault(_half("h", (A, B), {}, validations={"c": truncated})) == "build truncated"
    assert dp.fault(_half("h", (A, B), {}, validations={"c": near_miss})) == ""
    assert dp.fault(_half("h", (A, B), {}, validations={"c": junk})) == ""  # format failures are the model's loss
    tries = {"k": [AttackAttempt(payload="?", output=TRUNCATED, tokens=1, truncated=True)]}
    assert dp.fault(_half("h", (A, B), {}, attempts=tries)) == "attack truncated"
    spoof = {"k": [AttackAttempt(payload="?", output=TRUNCATED, tokens=1)]}  # challenge stdout, not the engine
    assert dp.fault(_half("h", (A, B), {}, attempts=spoof)) == ""
    st = _state()
    halves = (_half("h1", (A, B), {"gpt-x": 60}, validations={"c": truncated}), _half("h2", (B, A), {}))
    board = dp.board_of(st, 1, "x", halves, 2)
    assert (board.void, board.diff, board.fault, board.replays) == (True, 0, "build truncated", 2)


def test_board_usd_sums_both_halves() -> None:
    halves = (_half("h1", (A, B), {}, usage=Usage(usd=0.25)), _half("h2", (B, A), {}, usage=Usage(usd=0.5)))
    assert dp.board_of(_state(), 1, "x", halves, 0).usd == pytest.approx(0.75)


def test_sign_test_and_bootstrap() -> None:
    assert ms.sign_test_p(6, 0) == pytest.approx(2 / 64)
    assert ms.sign_test_p(3, 3) == 1.0
    assert ms.sign_test_p(0, 0) == 1.0
    lo, hi = ms.bootstrap_ci([10, 20, -5, 30], "seed")
    assert (lo, hi) == ms.bootstrap_ci([10, 20, -5, 30], "seed")  # seeded, replayable
    assert lo <= 13.75 <= hi
    assert ms.bootstrap_ci([], "s") == (0.0, 0.0)


def test_sprt_matches_the_closed_form() -> None:
    lower, upper = ms.sprt_bounds(0.05, 0.05)
    assert lower == pytest.approx(math.log(0.05 / 0.95))
    assert upper == pytest.approx(math.log(0.95 / 0.05))
    assert ms.llr(8, 0, 0.75) == pytest.approx(8 * math.log(1.5))
    assert ms.sprt(8, 0, 0.75, 0.05, 0.05) == "first"
    assert ms.sprt(0, 8, 0.75, 0.05, 0.05) == "second"
    assert ms.sprt(5, 5, 0.75, 0.05, 0.05) == "continue"
    assert ms.sprt(11, 11, 0.75, 0.05, 0.05) == "neither"


def test_stop_rule_and_result() -> None:
    assert not _state([10, 10, 10]).done()  # below boards_min
    sweep = _state([10] * 8)
    assert sweep.done()
    assert (sweep.result(), sweep.winner()) == ("first", "gpt-x")
    assert _state([-10] * 8).winner() == "claude-y"
    even = _state([10, -10] * 4)
    assert even.done()
    assert (even.result(), even.winner()) == ("inconclusive", None)  # spend never breaks the tie
    with pytest.raises(ValueError, match="not finished"):
        _state([10]).result()
    fixed = _state([10] * 4, dp.DuplicateRule(stop="none", boards_min=4, boards_max=6))
    assert not fixed.done()


def test_too_many_void_boards_is_void() -> None:
    st = _state([10] * 5)
    for i in range(3):
        st.boards.append(dp.Board(board_no=6 + i, seed="x", halves=("a", "b"), raw=0, diff=0, void=True))
    assert st.void_share_exceeded()  # 3 of 8 > 25%
    assert st.result() == "void"
    st.boards[5] = dp.Board(board_no=6, seed="x", halves=("a", "b"), raw=1, diff=1)
    assert not st.void_share_exceeded()  # 2 of 8 is not over 25%


def test_rule_validates_its_range() -> None:
    with pytest.raises(ValueError, match="boards_min"):
        dp.DuplicateRule(boards_min=9, boards_max=8)


def _created() -> Event:
    data = {
        "seed": "s",
        "rule": RULE.model_dump(mode="json"),
        "agents": [A.model_dump(mode="json"), B.model_dump(mode="json")],
        "rules": Rules().dump(),
        "limits": SandboxLimits().model_dump(mode="json"),
    }
    return Event(v=1, seq=0, type=dp.DUPLICATE_CREATED, trace_id="d", data=data)


def test_fold_replays_boards_in_order() -> None:
    board = dp.Board(board_no=1, seed="x", halves=("a", "b"), raw=5, diff=5)
    scored = Event(v=1, seq=1, type=dp.BOARD_SCORED, trace_id="d", data=board.model_dump())
    st = dp.fold("d", [_created(), scored])
    assert st.boards == [board]
    late = Event(v=1, seq=1, type=dp.BOARD_SCORED, trace_id="d", data={**board.model_dump(), "board_no": 2})
    with pytest.raises(EventLogCorruptError, match="out of order"):
        dp.fold("d", [_created(), late])
    with pytest.raises(EventLogCorruptError, match="unexpected"):
        dp.fold("d", [_created(), Event(v=1, seq=1, type="nope", trace_id="d", data={})])


def test_capped_ends_the_match_on_played_boards() -> None:
    capped = Event(v=1, seq=1, type=dp.DUPLICATE_CAPPED, trace_id="d", data={})
    st = dp.fold("d", [_created(), capped])
    assert st.capped
    assert st.done()
    assert st.result() == "inconclusive"
    late = Event(v=1, seq=2, type=dp.BOARD_SCORED, trace_id="d", data={})
    with pytest.raises(EventLogCorruptError, match="unexpected"):
        dp.fold("d", [_created(), capped, late])
    st = _state([1])
    st.boards[0] = st.boards[0].model_copy(update={"usd": 0.4})
    assert dp.next_board_usd(st) == 0.4
    assert dp.next_board_usd(_state()) == 0.0


def test_void_scope_matches_the_labeled_failures() -> None:
    """The 140 labeled failures (failure-labels-2026-11-05): exactly the 8 harness faults void, the rest score."""
    rows = list(csv.DictReader((FIXTURES / "failure-labels-2026-11-05.csv").open(encoding="utf-8")))
    assert len(rows) == 140
    for row in rows:
        reason = row["reason"]
        if row["event_type"] == "challenge_validated_invalid":
            voids = dp.build_fault(reason)
        else:  # the engine sets ``truncated`` exactly when it writes the sentinel itself
            voids = dp.attack_fault(AttackAttempt(payload="?", output=reason, tokens=1, truncated=reason == TRUNCATED))
        assert voids == (row["category"] == "harness_fault_or_truncation"), row
    assert sum(r["category"] == "harness_fault_or_truncation" for r in rows) == 8
    assert not dp.build_fault("malformed: no JSON object found")  # format failures after retries stay scored
