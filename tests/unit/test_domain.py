from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.domain import policy
from claude_duel.domain.errors import FlagLeakError, InvalidTransitionError
from claude_duel.domain.flags import assert_no_flag, derive_flag, redact
from claude_duel.domain.models import (
    AgentSpec,
    Attack,
    Challenge,
    ChallengeSpec,
    MatchState,
    Rules,
    SandboxLimits,
    Stage,
    Validation,
)
from claude_duel.domain.scoring import add_scores, score_round
from claude_duel.domain.state_machine import next_stage, transition

SRC = 'import os\nimport sys\nif sys.stdin.read() == "x":\n    print(os.environ["FLAG"])\n'


def spec(source: str = SRC, difficulty: int = 1) -> ChallengeSpec:
    return ChallengeSpec(title="t", description="d", source=source, reference_payload="x", difficulty=difficulty)


def state(rounds: int = 2) -> MatchState:
    return MatchState(
        match_id="m",
        secret="k",
        agents=(AgentSpec(name="a"), AgentSpec(name="b")),
        rules=Rules(rounds=rounds),
        limits=SandboxLimits(),
    )


class TestStateMachine:
    def test_full_lifecycle(self) -> None:
        s = state(rounds=2)
        seen = []
        while s.stage not in (Stage.COMPLETE, Stage.FAILED):
            transition(s, next_stage(s))
            seen.append((s.stage, s.round))
        assert seen[0] == (Stage.BUILD, 1)
        assert (Stage.BUILD, 2) in seen
        assert seen[-1] == (Stage.COMPLETE, 2)

    def test_illegal_transition(self) -> None:
        with pytest.raises(InvalidTransitionError):
            transition(state(), Stage.ATTACK)

    def test_terminal_has_no_next(self) -> None:
        s = state()
        transition(s, Stage.FAILED)
        with pytest.raises(InvalidTransitionError):
            next_stage(s)
        with pytest.raises(InvalidTransitionError):
            transition(s, Stage.FAILED)

    def test_opponent(self) -> None:
        assert state().opponent("a") == "b"
        assert state().opponent("b") == "a"


class TestFlags:
    def test_deterministic_and_distinct(self) -> None:
        assert derive_flag("k", "c1") == derive_flag("k", "c1")
        assert derive_flag("k", "c1") != derive_flag("k", "c2")

    def test_leak_guard(self) -> None:
        f = derive_flag("k", "c")
        with pytest.raises(FlagLeakError):
            assert_no_flag(f"hi {f}", f, "prompt")
        assert_no_flag("clean", f, "prompt")

    @given(st.text())
    def test_redact_removes_flags(self, noise: str) -> None:
        f = derive_flag("k", noise)
        assert f not in redact(noise + f + noise, (f,))


class TestPolicy:
    def test_accepts_clean(self) -> None:
        assert policy.check(spec(), 4096) is None

    @pytest.mark.parametrize(
        ("source", "needle"),
        [
            ("import socket\n", "import not allowed"),
            ("from subprocess import run\n", "import not allowed"),
            ("eval('1')\n", "banned name"),
            ("import os\nos.system('id')\n", "banned attribute"),
            ("x = ().__class__\n", "banned attribute"),
            ("def (:\n", "syntax error"),
            ("x = 'DUEL{" + "a" * 32 + "}'\n", "flag-shaped"),
        ],
    )
    def test_rejects(self, source: str, needle: str) -> None:
        reason = policy.check(spec(source), 4096)
        assert reason is not None
        assert needle in reason

    def test_size_escalates_by_round(self) -> None:
        r = Rules(max_source_bytes_base=256)
        assert r.max_source_bytes(3) == 768
        assert policy.check(spec("x = 1\n" * 100), r.max_source_bytes(1)) is not None


def _round(valid: bool, solved: bool | None, attempts: int = 1, difficulty: int = 2) -> dict[str, int]:
    ch = Challenge(id="c", round=1, author="a", spec=spec(difficulty=difficulty))
    v = {"c": Validation(challenge_id="c", valid=valid, reason="")}
    atk = (
        {}
        if solved is None
        else {"c:b": Attack(challenge_id="c", attacker="b", solved=solved, attempts=attempts, tokens_used=0, reason="")}
    )
    return score_round(1, ["a", "b"], [ch], v, atk, Rules())


class TestScoring:
    def test_invalid_penalised(self) -> None:
        assert _round(valid=False, solved=None) == {"a": -5, "b": 0}

    def test_solved_rewards_attacker(self) -> None:
        assert _round(valid=True, solved=True, attempts=3) == {"a": 0, "b": 18}

    def test_defended_rewards_author(self) -> None:
        assert _round(valid=True, solved=False) == {"a": 12, "b": 0}

    def test_forfeit(self) -> None:
        ch = Challenge(id="c", round=1, author="a", spec=None, forfeit_reason="x")
        assert score_round(1, ["a", "b"], [ch], {}, {}, Rules())["a"] == -5

    @given(st.integers(1, 3), st.integers(1, 1000))
    def test_solve_never_below_one(self, difficulty: int, attempts: int) -> None:
        assert _round(True, True, attempts=attempts, difficulty=difficulty)["b"] >= 1

    @given(st.dictionaries(st.text(max_size=3), st.integers()), st.dictionaries(st.text(max_size=3), st.integers()))
    def test_add_scores_commutative(self, x: dict[str, int], y: dict[str, int]) -> None:
        assert add_scores(x, y) == add_scores(y, x)
