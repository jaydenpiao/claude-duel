"""Memory token budget, line-boundary source cuts and the reveal rule (design-memory-strategy)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from claude_duel.domain import memory
from claude_duel.domain.flags import derive_flag
from claude_duel.domain.models import (
    AgentSpec,
    Attack,
    AttackAttempt,
    Challenge,
    ChallengeSpec,
    MatchState,
    MemoryRule,
    Rules,
    SandboxLimits,
    Validation,
)

SOLVER = "print('solver-secret-xyz')"
REF = "reference-payload-xyz"


def _source(n: int, tag: str) -> str:
    return "\n".join(f"x_{tag}_{i} = 'é€{i}'  # line {i}" for i in range(n))


def _state(sources: dict[int, list[int]], scored: list[int], current: int) -> MatchState:
    """bob authors challenges per round with the given line counts; alice attacks and captures each."""
    challenges, validations, attacks, attempts = {}, {}, {}, {}
    for rnd, sizes in sources.items():
        for i, n in enumerate(sizes):
            cid = f"r{rnd}-b{i}"
            spec = ChallengeSpec(
                title=f"t{cid}",
                description="",
                source=_source(n, cid),
                reference_payload=REF,
                solver=SOLVER,
                difficulty=2,
            )
            challenges[cid] = Challenge(id=cid, round=rnd, author="bob", spec=spec)
            validations[cid] = Validation(challenge_id=cid, valid=True, reason="ok")
            attacks[f"{cid}:alice"] = Attack(
                challenge_id=cid, attacker="alice", solved=True, attempts=1, tokens_used=1, reason="ok"
            )
            attempts[f"{cid}:alice"] = [AttackAttempt(payload=f"pay-{cid}", output="<flag captured>", tokens=1)]
    return MatchState(
        match_id="m",
        secret="s" * 64,
        agents=(AgentSpec(name="alice"), AgentSpec(name="bob")),
        rules=Rules(),
        limits=SandboxLimits(),
        round=current,
        challenges=challenges,
        validations=validations,
        attacks=attacks,
        attempts=attempts,
        scored_rounds=scored,
        scores={"alice": 2, "bob": 0},
    )


RULE = MemoryRule(kind="summary")


def test_est_tokens_is_utf8_bytes_over_four() -> None:
    assert memory.est_tokens("") == 0
    assert memory.est_tokens("abcd") == 1
    assert memory.est_tokens("abcde") == 2
    assert memory.est_tokens("€") == 1  # 3 bytes


def test_long_source_is_cut_at_a_line_with_marker() -> None:
    state = _state({1: [400]}, scored=[1], current=2)
    text = memory.summarize(state, "alice", 1, RULE)
    assert "# [truncated: " in text
    assert "of 400 lines shown]" in text
    body = text.split("  source:\n", 1)[1].split("\n# [truncated")[0]
    assert all(line.startswith("x_r1-b0_") for line in body.splitlines())  # whole lines only
    assert memory.est_tokens(body) <= RULE.source_tokens


def test_exhausted_source_budget_omits_the_rest() -> None:
    rule = MemoryRule(kind="summary", max_tokens=8000, source_tokens=100)
    state = _state({1: [40, 40, 40]}, scored=[1], current=2)
    text = memory.summarize(state, "alice", 1, rule)
    assert memory.SOURCE_OMITTED in text
    assert "'tr1-b0'" in text
    assert "'tr1-b2'" in text


def test_source_hidden_until_round_scored() -> None:
    unscored = _state({1: [3]}, scored=[], current=2)
    assert "source:" not in memory.summarize(unscored, "alice", 1, RULE)
    scored = _state({1: [3]}, scored=[1], current=2)
    assert "source:" in memory.summarize(scored, "alice", 1, RULE)


def test_author_never_sees_own_source_and_no_secrets_leak() -> None:
    state = _state({1: [30], 2: [30]}, scored=[1, 2], current=3)
    mine = memory.summarize(state, "bob", 2, RULE)
    assert "source:" not in mine
    for agent in ("alice", "bob"):
        text = memory.summarize(state, agent, 2, MemoryRule(kind="summary", max_tokens=150, source_tokens=60))
        assert SOLVER not in text
        assert "solver-secret" not in text
        assert REF not in text
        assert all(derive_flag(state.secret, cid) not in text for cid in state.challenges)


def test_drop_order_keeps_scoreboard_and_latest_round() -> None:
    state = _state({1: [20], 2: [20], 3: [20]}, scored=[1, 2, 3], current=4)
    text = memory.summarize(state, "alice", 3, MemoryRule(kind="summary", max_tokens=100, source_tokens=0))
    assert text.startswith(memory.OMITTED)
    assert "Round 3:" in text
    assert "Round 1:" not in text
    assert "Scoreboard: alice 2, bob 0" in text


def test_payloads_drop_before_sources() -> None:
    state = _state({1: [2]}, scored=[1], current=2)
    full = memory.summarize(state, "alice", 1, MemoryRule(kind="summary", max_tokens=8000))
    budget = memory.est_tokens(full) - 1
    text = memory.summarize(state, "alice", 1, MemoryRule(kind="summary", max_tokens=max(100, budget)))
    if budget >= 100:
        assert "pay-r1-b0" not in text
        assert "source:" in text


def test_legacy_char_rule_still_loads() -> None:
    rule = MemoryRule.model_validate({"kind": "summary", "max_chars": 3000, "source_chars": 600})
    assert (rule.max_tokens, rule.source_tokens) == (750, 150)


@settings(max_examples=40, deadline=None)
@given(
    st.dictionaries(st.integers(1, 4), st.lists(st.integers(1, 120), min_size=1, max_size=3), min_size=1),
    st.integers(100, 2000),
    st.integers(0, 1500),
)
def test_budget_holds_for_any_history(sizes: dict[int, list[int]], max_tokens: int, source_tokens: int) -> None:
    upto = max(sizes)
    state = _state(sizes, scored=sorted(sizes), current=upto + 1)
    rule = MemoryRule(kind="summary", max_tokens=max_tokens, source_tokens=source_tokens)
    text = memory.summarize(state, "alice", upto, rule)
    assert memory.est_tokens(text) <= max_tokens
    text.encode("utf-8")  # never a split code point
    cut = memory.cut_sources(state, "alice", upto, rule)
    bodies = [s.split("\n# [")[0] for s in cut.values() if s != memory.SOURCE_OMITTED]
    assert sum(memory.est_tokens(b) for b in bodies) <= source_tokens


def test_dict_order_does_not_matter() -> None:
    state = _state({1: [30, 30], 2: [30]}, scored=[1, 2], current=3)
    flipped = state.model_copy(
        update={
            "challenges": dict(reversed(state.challenges.items())),
            "attacks": dict(reversed(state.attacks.items())),
        }
    )
    assert memory.summarize(state, "alice", 2, RULE) == memory.summarize(flipped, "alice", 2, RULE)


def test_same_bytes_across_hash_seeds() -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from test_memory_budget import _state, RULE; "
        "from claude_duel.domain import memory; "
        "sys.stdout.write(memory.summarize(_state({1: [30, 30], 2: [30]}, [1, 2], 3), 'alice', 2, RULE))"
    )
    here = str(Path(__file__).parent)
    outs = {
        subprocess.run(
            [sys.executable, "-c", code, here],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            check=True,
        ).stdout
        for seed in ("1", "2")
    }
    assert len(outs) == 1
