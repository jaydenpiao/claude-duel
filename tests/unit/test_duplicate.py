"""Duplicate play core: board seeds, mirrored halves, clip, harness-fault voids, stats and the stop rule ($0)."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import pytest

from claude_duel.domain import duplicate as dp
from claude_duel.domain import match_stats as ms
from claude_duel.domain import modes
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import (
    TRUNCATED,
    AgentSpec,
    AttackAttempt,
    Challenge,
    Event,
    MatchState,
    Rules,
    SandboxLimits,
    Usage,
    Validation,
)

A, B = AgentSpec(name="gpt-x"), AgentSpec(name="claude-y")
RULE = dp.DuplicateRule(boards_min=4, boards_max=8, stop="sprt")  # the older win-count rule; margin tests below
MARGIN = dp.DuplicateRule()

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


def _forfeit(reason: str, truncated: bool) -> dict[str, Any]:
    """A half whose challenge "c" was forfeited at build time with ``reason``; ``truncated`` is the typed flag."""
    return {
        "challenges": {"c": Challenge(id="c", round=1, author="gpt-x", spec=None, forfeit_reason=reason)},
        "validations": {"c": Validation(challenge_id="c", valid=False, reason=reason)},
        "build_truncated": ["c"] if truncated else [],
    }


def test_truncation_is_scored_like_a_refusal_and_counted_never_voided() -> None:
    cut = _forfeit(f"malformed: {TRUNCATED}", truncated=True)
    assert dp.fault(_half("h", (A, B), {}, **cut)) == ""
    assert dp.truncations(_half("h", (A, B), {}, **cut)) == {"gpt-x": 1}
    quoted = _forfeit(f"malformed: {TRUNCATED}", truncated=False)  # the text alone is never counted
    assert dp.truncations(_half("h", (A, B), {}, **quoted)) == {}
    near_miss = _forfeit("reference payload does not reveal flag", truncated=False)
    assert dp.fault(_half("h", (A, B), {}, **near_miss)) == ""
    junk = _forfeit("malformed: no JSON object found", truncated=False)
    assert dp.fault(_half("h", (A, B), {}, **junk)) == ""  # format failures are the model's loss
    tries = {"c:claude-y": [AttackAttempt(payload="?", output=TRUNCATED, tokens=1, truncated=True)]}
    assert dp.fault(_half("h", (A, B), {}, attempts=tries)) == ""
    assert dp.truncations(_half("h", (A, B), {}, attempts=tries)) == {"claude-y": 1}
    spoof = {"c:claude-y": [AttackAttempt(payload="?", output=TRUNCATED, tokens=1)]}  # challenge stdout
    assert dp.truncations(_half("h", (A, B), {}, attempts=spoof)) == {}
    st = _state()
    halves = (_half("h1", (A, B), {"gpt-x": 60}, **cut), _half("h2", (B, A), {}))
    board = dp.board_of(st, 1, "x", halves, 0)
    assert (board.void, board.diff, board.fault, board.replays) == (False, 60, "", 0)  # scored: no re-roll
    assert board.truncated == {"gpt-x": 1}


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


def test_sign_flip_p_is_exact() -> None:
    assert ms.sign_flip_p([10] * 6) == pytest.approx(2 / 64)  # only the two all-same-sign flips are as extreme
    assert ms.sign_flip_p([]) == 1.0
    assert ms.sign_flip_p([0, 0]) == 1.0
    assert ms.sign_flip_p([30, -10]) == 1.0  # every flip sums to |20| or |40|: nothing is unusual
    assert ms.sign_flip_p([30, 10]) == pytest.approx(0.5)  # +-40 is as extreme as observed, +-20 is not
    xs = [12, -3, 7, 20, -5, 9]
    brute = sum(abs(sum(x if (m >> i) & 1 else -x for i, x in enumerate(xs))) >= sum(xs) for m in range(64)) / 64
    assert ms.sign_flip_p(xs) == pytest.approx(brute)


def test_margin_rule_calls_on_summed_margin_not_win_count() -> None:
    assert (MARGIN.stop, MARGIN.boards_min, MARGIN.boards_max) == ("margin", 12, 48)
    assert not _state([60] * 11, MARGIN).done()  # below boards_min
    rout = _state([60] * 12, MARGIN)
    assert rout.done()
    assert rout.result() == "first"  # p = 2 / 4096, under early_alpha
    assert dp.stop_now(MARGIN, 13, [60] * 13) is False  # looks are every 2 boards past boards_min
    # fewer wins but bigger margins: the sign test sees 6-6, the margin test sees the gap
    big_small = [40, 40, 40, 40, 40, 40, -5, -5, -5, -5, -5, -5] * 4
    st = _state(big_small, MARGIN)
    assert st.done()
    assert st.result() == "first"
    close = _state([10, -10] * 24, MARGIN)
    assert close.done()
    assert close.result() == "inconclusive"  # too close to call: no winner at boards_max


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
    """The 140 labeled failures (failure-labels-2026-11-05): exactly the 8 truncations are flagged (and counted), and
    none of the 140 voids a board: all are scored."""
    rows = list(csv.DictReader((FIXTURES / "failure-labels-2026-11-05.csv").open(encoding="utf-8")))
    assert len(rows) == 140
    for row in rows:
        reason = row["reason"]  # the engine sets the typed flag exactly when it wrote the sentinel itself
        if row["event_type"] == "challenge_validated_invalid":
            half = _half("h", (A, B), {}, **_forfeit(reason, truncated=reason == f"malformed: {TRUNCATED}"))
            voids = dp.build_fault(half, "c")
            assert dp.fault(half) == ""
        else:
            voids = dp.attack_fault(AttackAttempt(payload="?", output=reason, tokens=1, truncated=reason == TRUNCATED))
        assert voids == (row["category"] == "harness_fault_or_truncation"), row
    assert sum(r["category"] == "harness_fault_or_truncation" for r in rows) == 8
