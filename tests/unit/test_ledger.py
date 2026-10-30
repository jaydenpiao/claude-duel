"""Spend ledger: cross-process totals, concurrent appends, torn lines, $0 entries."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, FAKE_MODEL, BudgetLimits, CostBudget
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.ports import Completion


def _spend(path: str, n: int) -> None:
    ledger = FileSpendLedger(Path(path))
    for _ in range(n):
        ledger.record("m", "gpt-4o-mini", 1, 0.25)


def test_other_instances_see_new_spend(tmp_path: Path) -> None:
    a, b = FileSpendLedger(tmp_path / "l.jsonl"), FileSpendLedger(tmp_path / "l.jsonl")
    assert b.total_usd() == 0.0
    a.record("m", "x", 1, 1.5)
    assert b.total_usd() == 1.5
    a.record("m", "x", 1, 1.0)
    assert b.total_usd() == 2.5


def test_concurrent_processes_never_lose_or_tear_entries(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_spend, args=(str(path), 50)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 200
    assert all(json.loads(line)["usd"] == 0.25 for line in lines)
    assert FileSpendLedger(path).total_usd() == pytest.approx(50.0)


def test_lifetime_cap_sees_another_process(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    budget = CostBudget(BudgetLimits(lifetime_max_usd=1.0), FileSpendLedger(path), DEFAULT_PRICES)
    budget.check_lifetime()
    _spend(str(path), 4)  # another process spends $1.00
    with pytest.raises(BudgetExceededError, match="lifetime"):
        budget.check_lifetime()


def test_torn_line_is_not_glued_to_next_entry(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ledger = FileSpendLedger(path)
    ledger.record("m", "x", 1, 1.0)
    with path.open("a") as fh:
        fh.write('{"torn": ')
    ledger.record("m", "x", 1, 2.0)
    assert FileSpendLedger(path).total_usd() == 3.0
    assert json.loads(path.read_text().splitlines()[-1])["usd"] == 2.0


def test_replaced_file_is_refolded(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ledger = FileSpendLedger(path)
    ledger.record("m", "x", 1, 5.0)
    path.write_text(json.dumps({"usd": 1.0}) + "\n")
    assert ledger.total_usd() == 1.0


def test_zero_cost_calls_are_ledgered(tmp_path: Path, new_state) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "l.jsonl"
    budget = CostBudget(BudgetLimits(), FileSpendLedger(path), DEFAULT_PRICES)
    budget.charge(new_state().match_id, FAKE_MODEL, Completion(text="", input_tokens=10, output_tokens=5))
    entry = json.loads(path.read_text())
    assert entry["usd"] == 0.0
    assert entry["model"] == FAKE_MODEL
    assert entry["tokens"] == 15


def test_spent_usd_counts_a_settled_reservation_once(tmp_path: Path) -> None:
    ledger = FileSpendLedger(tmp_path / "ledger.jsonl")
    rid = ledger.reserve("s1-g1", "gpt-4o-mini", 0.05)
    assert ledger.spent_usd("s1-") == pytest.approx(0.05)
    ledger.record("s1-g1", "gpt-4o-mini", 100, 0.01, settles=rid)
    ledger.record("other", "gpt-4o-mini", 100, 0.02)
    assert ledger.spent_usd("s1-") == pytest.approx(0.01)
    assert ledger.total_usd() == pytest.approx(0.03)
