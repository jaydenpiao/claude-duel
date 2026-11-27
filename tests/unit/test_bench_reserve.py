"""S18-2 regression: `duel bench` calls are reserved before they run, so a timed-out call is ledgered at its
reservation (likely billed, S17-1) and a failed call settles at $0. No model calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.bench import MeteredProvider
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.domain.errors import ProviderTimeoutError
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.ports import Completion, CompletionRequest

REQ = CompletionRequest(system="s", prompt="p" * 4000, purpose="attack", max_tokens=1500)


class Raises:
    name = "fake"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def complete(self, request: CompletionRequest) -> Completion:
        raise self.exc


def _setup(tmp_path: Path, inner: Raises) -> tuple[MeteredProvider, CostBudget, MatchState, Path]:
    path = tmp_path / "ledger.jsonl"
    budget = CostBudget(BudgetLimits(), FileSpendLedger(path), {**DEFAULT_PRICES, "fake-1": (5.0, 20.0)})
    agents = (AgentSpec(name="alice", model="fake-1"), AgentSpec(name="bob", model="fake-1"))
    state = MatchState(match_id="bench-x", agents=agents, rules=Rules(), limits=SandboxLimits())
    return MeteredProvider(inner, "fake-1", budget, state), budget, state, path


def test_a_bench_timeout_is_ledgered_at_its_reservation(tmp_path: Path) -> None:
    p, budget, state, path = _setup(tmp_path, Raises(ProviderTimeoutError("openai timeout")))
    with pytest.raises(ProviderTimeoutError):
        p.complete(REQ)
    lines = path.read_text().splitlines()
    assert sum('"reserve"' in line for line in lines) == 1
    assert sum('"settles"' in line for line in lines) == 1
    assert FileSpendLedger(path).spent_usd("bench-x") > 0
    assert budget.run_usd == pytest.approx(FileSpendLedger(path).spent_usd("bench-x"))
    assert state.usage.calls == 1


def test_a_failed_bench_call_settles_at_zero(tmp_path: Path) -> None:
    p, budget, _, path = _setup(tmp_path, Raises(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        p.complete(REQ)
    assert FileSpendLedger(path).spent_usd("bench-x") == 0
    assert budget.run_usd == 0
