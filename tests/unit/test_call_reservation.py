"""a provider call is reserved in the ledger before it runs, so a crash before the charge is still counted."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, FAKE_MODEL, BudgetLimits, CostBudget, SpendCap
from claude_duel.app.container import build_ledger
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest

PRICES = {**DEFAULT_PRICES, FAKE_MODEL: (1.0, 2.0)}
REQ = CompletionRequest(system="s" * 100, prompt="p" * 900, purpose="build", max_tokens=1000)


def _budget(path: Path) -> CostBudget:
    return CostBudget(BudgetLimits(), FileSpendLedger(path), PRICES)


def test_crash_between_call_and_charge_stays_counted(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    _budget(path).reserve("m1", FAKE_MODEL, REQ)  # process dies here, before charge
    assert FileSpendLedger(path).total_usd() == pytest.approx((1000 * 1.0 + 1000 * 2.0) / 1e6)


def test_charge_replaces_the_reservation(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    budget = _budget(path)
    rid = budget.reserve("m1", FAKE_MODEL, REQ)
    budget.charge("m1", FAKE_MODEL, Completion(text="x", input_tokens=10, output_tokens=5), rid)
    assert FileSpendLedger(path).total_usd() == pytest.approx((10 * 1.0 + 5 * 2.0) / 1e6)
    budget.release("m1", FAKE_MODEL, budget.reserve("m1", FAKE_MODEL, REQ))
    assert FileSpendLedger(path).total_usd() == pytest.approx(20 / 1e6)


def test_engine_failure_releases_and_success_settles(tmp_path: Path) -> None:

    settings = Settings(store_dir=tmp_path, sandbox="subprocess")
    play(settings, "m1", InMemoryMetrics())
    ledger = build_ledger(settings)
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert sum('"reserve"' in line for line in lines) == sum('"settles"' in line for line in lines) > 0
    assert ledger.total_usd() >= 0


def test_hard_stop_refuses_a_call_whose_worst_case_could_cross_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    budget = _budget(path)
    worst = (1000 * 1.0 + 1000 * 2.0) / 1e6
    budget.hard_caps = (SpendCap("match", "lad-s1-", 2.5 * worst),)
    budget.reserve("lad-s1-b1a", FAKE_MODEL, REQ)
    budget.reserve("lad-s10-b1a", FAKE_MODEL, REQ)  # another step's prefix does not count
    budget.reserve("lad-s1-b1b", FAKE_MODEL, REQ)
    with pytest.raises(BudgetExceededError, match="hard stop: match"):
        budget.reserve("lad-s1-b2a", FAKE_MODEL, REQ)
    assert FileSpendLedger(path).spent_usd("lad-s1-") == pytest.approx(2 * worst)  # the refused call left no trace
