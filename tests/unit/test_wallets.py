"""Per-agent wallets and the efficiency scorer (#35)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import allowance
from claude_duel.app.budget import BudgetLimits
from claude_duel.app.replay import replay
from claude_duel.domain.errors import BudgetExceededError, WalletEmptyError
from claude_duel.domain.models import ScoringRule, Usage
from claude_duel.domain.scoring import efficiency_award
from claude_duel.ports import Completion, CompletionRequest


class Counter:
    name = "counter"

    def __init__(self, skill: int) -> None:
        self.fake, self.calls = FakeProvider(skill=skill), 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        return self.fake.complete(request)


def test_empty_wallet_forfeits_builds_and_stops_attacks(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Counter(2), Counter(5)
    state = new_state(rounds=2, wallet={"per_agent_tokens": 1})
    state = make_engine({"alice": alice, "bob": bob}).run(state)
    assert alice.calls == 1  # the first call starts under the wallet; every later one is refused
    assert bob.calls == 1
    forfeits = [c for c in state.challenges.values() if c.spec is None]
    assert len(forfeits) >= 3
    assert all("wallet empty" in (c.forfeit_reason or "") for c in forfeits)
    assert state.usage_by_agent["alice"].calls == 1
    assert replay(state, event_log.read(state.match_id)).consistent


def test_match_cap_binds_before_a_larger_wallet(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine(budget=BudgetLimits(match_max_tokens=1))
    with pytest.raises(BudgetExceededError, match="match tokens"):
        engine.run(new_state(wallet={"per_agent_tokens": 10_000_000}))


def test_usd_wallet_and_check(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state(wallet={"per_agent_usd": 0.01})
    assert allowance.empty(state, "alice") is None
    allowance.check(state, "alice")
    state.usage_by_agent["alice"] = Usage(calls=3, tokens=9, usd=0.02)
    with pytest.raises(WalletEmptyError, match=r"spent \$0.0200/\$0.01"):
        allowance.check(state, "alice")
    assert allowance.empty(state, "bob") is None


def test_no_wallet_is_unlimited(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state()
    state.usage_by_agent["alice"] = Usage(tokens=10**9, usd=999)
    assert allowance.empty(state, "alice") is None


@given(full=st.integers(1, 500), a=st.integers(0, 10**6), b=st.integers(0, 10**6))
def test_efficiency_award_is_monotone_and_at_least_one(full: int, a: int, b: int) -> None:
    rule = ScoringRule(kind="efficiency")
    lo, hi = sorted((a, b))
    assert efficiency_award(full, hi, rule) <= efficiency_award(full, lo, rule)
    assert efficiency_award(full, hi, rule) >= 1
    assert efficiency_award(full, 0, rule) == full


def test_efficiency_match_discounts_captures_only(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    classic = make_engine().run(new_state("c"))
    eff = make_engine().run(new_state("e", scoring={"kind": "efficiency", "max_penalty": 1000}))
    assert replay(eff, event_log.read("e")).consistent
    assert any(a.solved for a in eff.attacks.values())
    assert sum(eff.scores.values()) < sum(classic.scores.values())
