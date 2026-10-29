"""Sealed Ledger step 2 wiring: allocation, stakes, replay, fallback, stake-0 skip, tiebreak, estimate."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.budget import BudgetLimits
from claude_duel.app.estimate import estimate
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.models import MatchState, Stage, Usage
from claude_duel.domain.modes import preset
from claude_duel.domain.series import game_winner
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest


class Allocating:
    """Fake provider whose allocation answer is fixed text; everything else is the plain fake."""

    name = "allocating"

    def __init__(self, text: str, skill: int = 2) -> None:
        self.inner, self.text, self.prompts = FakeProvider(skill=skill), text, []  # type: ignore[var-annotated]

    def complete(self, request: CompletionRequest) -> Completion:
        self.prompts.append((request.purpose, request.prompt))
        if request.purpose == "allocate":
            return Completion(text=self.text, input_tokens=10, output_tokens=5)
        return self.inner.complete(request)


def _ledger(new_state, match_id: str = "l1") -> MatchState:  # type: ignore[no-untyped-def]
    state: MatchState = new_state(match_id)
    state.rules = preset("ledger").model_copy(update={"rounds": 2})
    return state


def test_ledger_match_completes_and_replays(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_ledger(new_state))
    assert state.stage is Stage.COMPLETE
    staked = [e for e in event_log.read("l1") if e.type == ev.STAKES_SET]
    assert [(e.data["round"], e.data["agent"]) for e in staked] == [(1, "alice"), (1, "bob"), (2, "alice"), (2, "bob")]
    assert all(e.data["stakes"] == [4000, 4000, 4000] and not e.data["fallback"] for e in staked)
    assert all(a.stake == 4000 for a in state.attacks.values())
    assert replay(state, event_log.read("l1")).consistent


def test_bad_allocation_falls_back_to_even_split(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    bad = {"alice": Allocating('{"stakes": [12000, 12000, 0]}'), "bob": Allocating("not json")}
    state = make_engine(providers=bad, metrics=metrics).run(_ledger(new_state))
    staked = [e for e in event_log.read("l1") if e.type == ev.STAKES_SET]
    assert all(e.data["fallback"] and e.data["stakes"] == [4000, 4000, 4000] for e in staked)
    assert metrics.counters["ledger.alloc_fallback"] == 4
    assert state.stage is Stage.COMPLETE


def test_stake_zero_skips_attack_and_caps_spend(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Allocating(json.dumps({"stakes": [12000, 0, 0]})), "bob": Allocating('{"stakes": [0, 0, 0]}')}
    state = make_engine(providers=provs).run(_ledger(new_state))
    by_attacker = {n: [a for a in state.attacks.values() if a.attacker == n] for n in ("alice", "bob")}
    assert all(a.stake == 0 and a.attempts == 0 and not a.solved for a in by_attacker["bob"])
    assert sum(a.stake == 0 for a in by_attacker["alice"]) == 2 * 2
    assert not any(p == "attack" and "AGENT: bob" in t for p, t in provs["bob"].prompts)
    assert all(a.tokens_used <= 12000 + 2000 for a in by_attacker["alice"])  # pre-call check: <= one call over
    assert replay(state, event_log.read("l1")).consistent


def test_allocate_prompt_shows_sources_and_never_the_flag(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Allocating('{"stakes": [4000, 4000, 4000]}'), "bob": Allocating('{"stakes": [4000, 4000, 4000]}')}
    make_engine(providers=provs).run(_ledger(new_state))
    prompt = next(t for p, t in provs["alice"].prompts if p == "allocate")
    for line in ("TASK: ALLOCATE", "BUDGET: 12000", "CHALLENGES: 3"):
        assert line in prompt
    assert "<<<UNTRUSTED" in prompt


def test_ledger_tie_goes_to_lower_spend_then_tokens(new_state) -> None:  # type: ignore[no-untyped-def]
    state = _ledger(new_state)
    state.scores = {"alice": 10, "bob": 10}
    state.usage_by_agent = {"alice": Usage(usd=0.002, tokens=900), "bob": Usage(usd=0.001, tokens=5000)}
    assert game_winner(state) == "bob"
    state.usage_by_agent = {"alice": Usage(usd=0.001, tokens=900), "bob": Usage(usd=0.001, tokens=5000)}
    assert game_winner(state) == "alice"
    state.usage_by_agent = {"alice": Usage(usd=0.001, tokens=900), "bob": Usage(usd=0.001, tokens=900)}
    assert game_winner(state) is None


def test_estimate_ledger_within_match_cap() -> None:
    limits = BudgetLimits()
    assert 0 < estimate(preset("ledger"), "gpt-5-nano", limits).usd <= limits.match_max_usd


def test_estimate_ledger_bounded_by_stakes() -> None:
    roomy = BudgetLimits(match_max_tokens=10_000_000, match_max_calls=10_000)
    row = estimate(preset("ledger"), "gpt-5-nano", roomy)
    plain = estimate(preset("ledger").model_copy(update={"ledger": None}), "gpt-5-nano", roomy)
    assert row.bound == "stakes"
    assert row.tokens < plain.tokens
