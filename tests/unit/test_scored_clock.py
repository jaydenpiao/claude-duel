"""Scored modes use a token clock, never wall-clock provider time (#67)."""

from __future__ import annotations

import time

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.domain.models import MatchState
from claude_duel.domain.modes import MODES
from claude_duel.ports import Completion, CompletionRequest

SCORED = sorted(n for n, r in MODES.items() if r.scoring.kind != "classic")


@pytest.mark.parametrize("name", SCORED)
def test_every_scored_preset_has_a_token_clock(name: str) -> None:
    clock = MODES[name].clock
    assert clock.bank_tokens is not None
    assert clock.bank_tokens > 0


class Laggy:
    """A fake model with real latency: the reply, and its tokens, are those of the fake."""

    name = "laggy"

    def __init__(self, skill: int, delay: float) -> None:
        self.fake, self.delay = FakeProvider(skill=skill), delay

    def complete(self, request: CompletionRequest) -> Completion:
        time.sleep(self.delay)
        return self.fake.complete(request)


@pytest.mark.parametrize("name", SCORED)
def test_latency_never_changes_a_scored_result(name: str, make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    rules = MODES[name].model_dump(exclude={"rounds", "challenges_per_round"})
    fast = make_engine({"alice": Laggy(2, 0.0), "bob": Laggy(5, 0.0)}).run(new_state("fast", rounds=1, **rules))
    slow = make_engine({"alice": Laggy(2, 0.02), "bob": Laggy(5, 0.0)}).run(new_state("slow", rounds=1, **rules))
    assert slow.usage.elapsed_ms > fast.usage.elapsed_ms
    assert slow.scores == fast.scores
    assert _outcomes(slow) == _outcomes(fast)
    assert {k: u.clock_tokens for k, u in slow.usage_by_agent.items()} == {
        k: u.clock_tokens for k, u in fast.usage_by_agent.items()
    }


def _outcomes(state: MatchState) -> list[tuple[str, bool, int, int, str]]:
    return sorted((a.attacker, a.solved, a.attempts, a.tokens_used, a.reason) for a in state.attacks.values())
