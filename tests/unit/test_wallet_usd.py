"""Dollar-unit efficiency, recorded price pins and the per-call output cap (#35)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import budget
from claude_duel.app.replay import replay
from claude_duel.domain.models import AgentSpec, Attack, ScoringRule
from claude_duel.domain.scoring import cost_used
from claude_duel.ports import Completion, CompletionRequest


class Spy:
    name = "spy"

    def __init__(self, skill: int) -> None:
        self.fake, self.caps = FakeProvider(skill=skill), set[int]()

    def complete(self, request: CompletionRequest) -> Completion:
        self.caps.add(request.max_tokens)
        return self.fake.complete(request)


def test_max_output_tokens_caps_every_call(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Spy(2), Spy(5)
    make_engine({"alice": alice, "bob": bob}).run(new_state(wallet={"max_output_tokens": 64}))
    assert alice.caps == bob.caps == {64}
    spy = Spy(2)
    make_engine({"alice": spy, "bob": Spy(5)}).run(new_state("plain"))
    assert spy.caps == {CompletionRequest.model_fields["max_tokens"].default}


def test_usd_unit_rescales_recorded_micros() -> None:
    atk = Attack(challenge_id="c", attacker="a", solved=True, attempts=1, tokens_used=10, reason="", usd_micros=3000)
    assert cost_used(atk, ScoringRule()) == 10
    assert cost_used(atk, ScoringRule(unit="usd", reference_usd_micros=1000, reference_tokens=4000)) == 12_000


def test_replay_ignores_later_price_changes(make_engine, new_state, event_log, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(budget.DEFAULT_PRICES, budget.FAKE_MODEL, (1.0, 2.0))
    rules = {"scoring": {"kind": "efficiency", "unit": "usd", "reference_usd_micros": 50}}
    state = make_engine().run(new_state(**rules))
    micros = [a.usd_micros for a in state.attacks.values()]
    assert micros
    assert all(m > 0 for m in micros)
    monkeypatch.setitem(budget.DEFAULT_PRICES, budget.FAKE_MODEL, (0.0, 0.0))
    result = replay(state, event_log.read(state.match_id))
    assert result.consistent


@pytest.mark.parametrize("unit", ["tokens", "usd"])
def test_old_attacks_default_to_zero_dollars(unit: str) -> None:
    atk = Attack.model_validate(
        {"challenge_id": "c", "attacker": "a", "solved": True, "attempts": 1, "tokens_used": 5, "reason": ""}
    )
    assert cost_used(atk, ScoringRule(unit=unit)) == (0 if unit == "usd" else 5)  # type: ignore[arg-type]


def test_agent_output_cap_wins_and_wallet_still_lowers_it(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Spy(2), Spy(5)
    state = new_state(max_output_tokens=900)
    a, b = state.agents
    state = state.model_copy(update={"agents": (a.model_copy(update={"max_output_tokens": 16_000}), b)})
    make_engine({"alice": alice, "bob": bob}).run(state)
    assert (alice.caps, bob.caps) == ({16_000}, {900})
    alice, bob = Spy(2), Spy(5)
    walleted = new_state("m2", wallet={"max_output_tokens": 64})
    walleted = walleted.model_copy(update={"agents": (a.model_copy(update={"max_output_tokens": 16_000}), b)})
    make_engine({"alice": alice, "bob": bob}).run(walleted)
    assert alice.caps == bob.caps == {64}


def test_agent_output_cap_is_bounded() -> None:
    with pytest.raises(ValidationError):
        AgentSpec(name="a", max_output_tokens=64_001)
    assert AgentSpec(name="a").max_output_tokens is None
