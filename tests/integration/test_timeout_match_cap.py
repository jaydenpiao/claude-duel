"""S18-1 regression: timed-out tries are ledgered (keep) and also folded into the match's own usage through a
call_timed_out event, so the per-match caps (ADR 0014 terminal CAPPED) see them across resumes. No model calls."""

from __future__ import annotations

import contextlib

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.app.engine import TIMEOUT_RETRIES, MatchEngine
from claude_duel.domain.errors import ProviderTimeoutError
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits, Stage
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest
from tests.helpers import any_source


class Stalls:
    name = "fake"

    def __init__(self) -> None:
        self.tries = 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.tries += 1
        raise ProviderTimeoutError("openai timeout")


def test_kept_timeouts_reach_the_match_cap(store, event_log, store_root) -> None:  # type: ignore[no-untyped-def]
    prices = {**DEFAULT_PRICES, "fake-1": (5.0, 20.0)}  # priced like a mid model, $/Mtok
    limits = BudgetLimits(match_max_usd=0.05, run_max_usd=5.0, run_max_calls=10_000)
    ledger_path = store_root / "ledger.jsonl"
    agents = (AgentSpec(name="alice", model="fake-1"), AgentSpec(name="bob", model="fake-1"))
    state = MatchState(
        match_id="cap-1",
        secret="s" * 64,
        agents=agents,
        rules=Rules(rounds=1, challenges_per_round=1),
        limits=SandboxLimits(),
    )
    inner = Stalls()
    for _resume in range(6):  # six resumes (each a fresh process in real life: run caps reset)
        budget = CostBudget(limits, FileSpendLedger(ledger_path), prices)
        alice = RetryingProvider(inner, InMemoryMetrics(), max_retries=4, sleep=lambda s: None)
        eng = MatchEngine(
            {"alice": alice, "bob": FakeProvider(skill=3)},
            SubprocessSandbox(any_source),
            store,
            event_log,
            InMemoryMetrics(),
            budget,
            {"alice": "fake-1", "bob": "fake-1"},
        )
        with contextlib.suppress(ProviderTimeoutError):
            state = eng.run(state)
    ledgered = FileSpendLedger(ledger_path).spent_usd("cap-1")
    final = store.load("cap-1")
    assert final.stage is Stage.CAPPED  # the kept tries reached the match cap
    assert inner.tries < 6 * (TIMEOUT_RETRIES + 1)  # resumes stop calling once capped
    assert final.usage.usd == pytest.approx(ledgered)  # the match's usage matches its ledger spend
    assert final.usage.calls == inner.tries
    tries = inner.tries
    budget = CostBudget(limits, FileSpendLedger(ledger_path), prices)
    eng = MatchEngine(
        {"alice": Stalls(), "bob": FakeProvider(skill=3)},
        SubprocessSandbox(any_source),
        store,
        event_log,
        InMemoryMetrics(),
        budget,
        {"alice": "fake-1", "bob": "fake-1"},
    )
    eng.run(store.load("cap-1"))  # a resume of the capped match makes no call
    assert inner.tries == tries
