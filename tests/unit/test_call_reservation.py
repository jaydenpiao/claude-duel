"""a provider call is reserved in the ledger before it runs, so a crash before the charge is still counted."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.adapters.store.replies import FileReplyStore
from claude_duel.app.budget import (
    DEFAULT_PRICES,
    FAKE_MODEL,
    REQUEST_FRAMING_TOKENS,
    BudgetLimits,
    CostBudget,
    SpendCap,
)
from claude_duel.app.container import build_ledger
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain.errors import BudgetExceededError, SpendLedgerCorruptError
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest

PRICES = {**DEFAULT_PRICES, FAKE_MODEL: (1.0, 2.0)}
REQ = CompletionRequest(system="s" * 100, prompt="p" * 900, purpose="build", max_tokens=1000)
IN = 1000 + REQUEST_FRAMING_TOKENS  # reserved input tokens for REQ
WORST = (IN * 1.0 + 1000 * 2.0) / 1e6


def _budget(path: Path) -> CostBudget:
    return CostBudget(BudgetLimits(), FileSpendLedger(path), PRICES, replies=FileReplyStore(path.parent / "replies"))


def test_crash_between_call_and_charge_stays_counted(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    _budget(path).reserve("m1", FAKE_MODEL, REQ)  # process dies here, before charge
    assert FileSpendLedger(path).total_usd() == pytest.approx(WORST)


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
    worst = WORST
    budget.hard_caps = (SpendCap("match", "lad-s1-", 2.5 * worst),)
    budget.reserve("lad-s1-b1a", FAKE_MODEL, REQ)
    budget.reserve("lad-s10-b1a", FAKE_MODEL, REQ)  # another step's prefix does not count
    budget.reserve("lad-s1-b1b", FAKE_MODEL, REQ)
    with pytest.raises(BudgetExceededError, match="hard stop: match"):
        budget.reserve("lad-s1-b2a", FAKE_MODEL, REQ)
    assert FileSpendLedger(path).spent_usd("lad-s1-") == pytest.approx(2 * worst)  # the refused call left no trace


def test_a_reply_bigger_than_its_reservation_is_charged_then_rejected(tmp_path: Path) -> None:
    """the settled cost is checked too. Over-size usage is ledgered (never under-reported) and stops the run."""
    path = tmp_path / "ledger.jsonl"
    budget = _budget(path)
    rid = budget.reserve("m1", FAKE_MODEL, REQ)
    with pytest.raises(BudgetExceededError, match="more than the reserved"):
        budget.charge("m1", FAKE_MODEL, Completion(text="x", input_tokens=10, output_tokens=1001), rid)
    assert FileSpendLedger(path).total_usd() == pytest.approx((10 * 1.0 + 1001 * 2.0) / 1e6)
    rid = budget.reserve("m2", FAKE_MODEL, REQ)
    with pytest.raises(BudgetExceededError, match="more than the reserved"):
        budget.charge("m2", FAKE_MODEL, Completion(text="x", input_tokens=IN + 1, output_tokens=1), rid)


def test_settled_spend_past_a_hard_cap_stops_after_the_charge(tmp_path: Path) -> None:
    """even inside its reservation, a charge that leaves a prefix over its cap stops the run."""
    path = tmp_path / "ledger.jsonl"
    other = _budget(path)
    budget = _budget(path)
    budget.hard_caps = (SpendCap("match", "lad-s1-", 1.5 * WORST),)
    rid = budget.reserve("lad-s1-b1a", FAKE_MODEL, REQ)
    other.charge("lad-s1-x", FAKE_MODEL, Completion(text="", input_tokens=IN, output_tokens=1000))  # uncapped writer
    with pytest.raises(BudgetExceededError, match="settled spend"):
        budget.charge("lad-s1-b1a", FAKE_MODEL, Completion(text="x", input_tokens=IN, output_tokens=1000), rid)


def test_racing_reservations_cannot_both_pass_a_cap(tmp_path: Path) -> None:
    """the spend read and the reserve append share one exclusive lock, so N racers fit only what the cap holds."""
    path = tmp_path / "ledger.jsonl"
    cap = SpendCap("match", "lad-s1-", 1.5 * WORST)  # room for one worst case
    gate = threading.Barrier(8)
    ok: list[str] = []
    refused: list[str] = []

    def race(i: int) -> None:
        budget = _budget(path)
        budget.hard_caps = (cap,)
        gate.wait()
        try:
            ok.append(budget.reserve(f"lad-s1-b{i}", FAKE_MODEL, REQ))
        except BudgetExceededError:
            refused.append(str(i))

    threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert (len(ok), len(refused)) == (1, 7)
    assert FileSpendLedger(path).spent_usd("lad-s1-") == pytest.approx(WORST)


@pytest.mark.parametrize("line", ['{"usd": -10.0}', '{"usd": NaN}', '{"usd": Infinity}', '{"usd": "x"}', "not json"])
def test_an_untrusted_ledger_line_refuses_spend(tmp_path: Path, line: str) -> None:
    """a negative or non-finite usd, or an unreadable line, is corruption: nothing more is spent."""
    path = tmp_path / "ledger.jsonl"
    FileSpendLedger(path).record("m", FAKE_MODEL, 1, 1.0)
    with path.open("a") as fh:
        fh.write(line + "\n")
    with pytest.raises(SpendLedgerCorruptError):
        FileSpendLedger(path).total_usd()
    with pytest.raises(SpendLedgerCorruptError):
        _budget(path).reserve("m2", FAKE_MODEL, REQ)


def test_a_torn_tail_refuses_spend(tmp_path: Path) -> None:
    """a torn last line (a crash mid-append) is corruption too, not silently skipped."""
    path = tmp_path / "ledger.jsonl"
    FileSpendLedger(path).record("m", FAKE_MODEL, 1, 1.0)
    with path.open("a") as fh:
        fh.write('{"usd": 5')
    with pytest.raises(SpendLedgerCorruptError, match="torn"):
        _budget(path).reserve("m2", FAKE_MODEL, REQ)


def test_one_attempt_key_holds_one_reservation_and_one_charge(tmp_path: Path) -> None:
    """a resumed attempt replaces its stale reservation, and a charged attempt's kept reply is reused."""
    path = tmp_path / "ledger.jsonl"
    _budget(path).reserve("m1", FAKE_MODEL, REQ, key="k1")  # crash after reserve, before the call returned
    budget = _budget(path)
    rid = budget.reserve("m1", FAKE_MODEL, REQ, key="k1")  # the resume: the stale reservation is released
    assert FileSpendLedger(path).total_usd() == pytest.approx(WORST)
    reply = Completion(text="answer", input_tokens=10, output_tokens=5)
    budget.charge("m1", FAKE_MODEL, reply, rid, key="k1")  # crash here, before CALL_CHARGED
    resumed = _budget(path)
    assert resumed.settled("k1") == reply  # reused: no second call, no second charge
    assert FileSpendLedger(path).total_usd() == pytest.approx((10 * 1.0 + 5 * 2.0) / 1e6)
    resumed.forget("k1")
    assert resumed.settled("k1") is None
    assert not list((tmp_path / "replies").iterdir())
