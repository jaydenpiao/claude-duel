from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.app.replay import replay
from claude_duel.domain.errors import (
    BudgetExceededError,
    MatchLockedError,
    PermanentProviderError,
    QuotaExceededError,
)
from claude_duel.domain.flags import derive_flag
from claude_duel.domain.models import Stage
from claude_duel.domain.state_machine import recover
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest


class Wrapped:
    """Fake provider with failure injection after N calls."""

    name = "wrapped"

    def __init__(
        self, inner: FakeProvider, fail_at: int | None = None, exc: Exception | None = None, text: str | None = None
    ) -> None:
        self.inner, self.fail_at, self.exc, self.text, self.calls = inner, fail_at, exc, text, 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at and self.exc is not None:
            raise self.exc
        if self.text is not None:
            return Completion(text=self.text, input_tokens=1, output_tokens=1)
        return self.inner.complete(request)


def test_full_match_is_complete_and_replayable(make_engine, new_state, store) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    state = make_engine(metrics=metrics).run(new_state())
    assert state.stage is Stage.COMPLETE
    assert state.round == 2
    assert len(state.challenges) == 8
    assert all(v.valid for v in state.validations.values())
    assert sum(a.solved for a in state.attacks.values()) > 0
    assert replay(state).consistent
    assert store.load("m1").scores == state.scores
    assert metrics.counters["unit.challenge_built"] == 8


def test_deterministic_across_runs(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    a = make_engine().run(new_state("a1"))
    b = make_engine().run(new_state("a1x"[:2]))
    assert a.scores.keys() == b.scores.keys()


def test_malformed_output_is_reprompted(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    engine = make_engine({"alice": FakeProvider(malformed_first=1), "bob": FakeProvider()}, metrics)
    state = engine.run(new_state())
    assert state.stage is Stage.COMPLETE
    assert metrics.counters["model.malformed"] == 1
    assert all(c.spec is not None for c in state.challenges.values())


def test_persistently_malformed_forfeits_and_wastes_attempts(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    junk = Wrapped(FakeProvider(), text="lol no")
    state = make_engine({"alice": junk, "bob": FakeProvider()}).run(new_state())
    alice = [c for c in state.challenges.values() if c.author == "alice"]
    assert all(c.spec is None and "malformed" in (c.forfeit_reason or "") for c in alice)
    assert state.scores["alice"] < 0
    assert all(not a.solved and a.attempts == 4 for a in state.attacks.values() if a.attacker == "alice")


def test_permanent_provider_error_forfeits_unit_only(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    broken = Wrapped(FakeProvider(), fail_at=1, exc=PermanentProviderError("400"))
    state = make_engine({"alice": broken, "bob": FakeProvider()}).run(new_state())
    assert state.stage is Stage.COMPLETE
    assert any("provider" in a.reason for a in state.attacks.values())


def test_quota_exceeded_aborts_and_resumes(make_engine, new_state, store) -> None:  # type: ignore[no-untyped-def]
    quota = Wrapped(FakeProvider(), fail_at=3, exc=QuotaExceededError("out of funds"))
    with pytest.raises(QuotaExceededError):
        make_engine({"alice": quota, "bob": FakeProvider()}).run(new_state())
    failed = store.load("m1")
    assert failed.stage is Stage.FAILED
    assert "out of funds" in (failed.failure or "")
    built_before = set(failed.challenges)
    recover(failed)
    done = make_engine().run(failed)
    assert done.stage is Stage.COMPLETE
    assert built_before <= set(done.challenges)
    assert replay(done).consistent


def test_crash_recovery_is_idempotent(make_engine, new_state, store) -> None:  # type: ignore[no-untyped-def]
    crash = Wrapped(FakeProvider(), fail_at=6, exc=RuntimeError("power cut"))
    with pytest.raises(RuntimeError):
        make_engine({"alice": FakeProvider(), "bob": crash}).run(new_state())
    saved = store.load("m1")
    calls_before = saved.usage.calls
    recover(saved)
    counter = Wrapped(FakeProvider(skill=5))
    done = make_engine({"alice": FakeProvider(), "bob": counter}).run(saved)
    assert done.stage is Stage.COMPLETE
    assert done.usage.calls > calls_before
    assert len([e for e in done.events if e.type == "challenge_built"]) == 8


def test_budget_breaker_trips(make_engine, new_state, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BudgetExceededError, match="match calls"):
        make_engine(budget=BudgetLimits(match_max_calls=3)).run(new_state())
    assert store.load("m1").stage is Stage.FAILED


def test_lifetime_ledger_refuses_new_matches(tmp_path: Path, new_state) -> None:  # type: ignore[no-untyped-def]
    ledger = FileSpendLedger(tmp_path / "ledger.jsonl")
    ledger.record("old", "gpt-4.1-nano", 10, 40.0)
    with (tmp_path / "ledger.jsonl").open("a") as fh:
        fh.write('{"torn": ')
    assert FileSpendLedger(tmp_path / "ledger.jsonl").total_usd() == 40.0

    engine = MatchEngine(
        {"alice": FakeProvider(), "bob": FakeProvider()},
        SubprocessSandbox(),
        FileMatchStore(tmp_path / "s"),
        InMemoryMetrics(),
        CostBudget(BudgetLimits(), ledger, DEFAULT_PRICES),
        {"alice": "fake-1", "bob": "fake-1"},
    )
    with pytest.raises(BudgetExceededError, match="lifetime"):
        engine.run(new_state())


def test_paid_calls_are_ledgered(tmp_path: Path, new_state) -> None:  # type: ignore[no-untyped-def]
    ledger = FileSpendLedger(tmp_path / "l.jsonl")
    budget = CostBudget(BudgetLimits(), ledger, DEFAULT_PRICES)
    state = new_state()
    budget.charge(state, "gpt-4.1-nano", Completion(text="", input_tokens=1_000_000, output_tokens=0))
    assert state.usage.usd == pytest.approx(0.10)
    assert json.loads((tmp_path / "l.jsonl").read_text())["model"] == "gpt-4.1-nano"
    run_budget = CostBudget(BudgetLimits(run_max_usd=0.05), ledger, DEFAULT_PRICES)
    run_budget.charge(new_state("x"), "gpt-4.1-nano", Completion(text="", input_tokens=1_000_000, output_tokens=0))
    with pytest.raises(BudgetExceededError, match="run cost"):
        run_budget.check(new_state("y"))


def test_concurrent_run_is_locked(make_engine, new_state, store) -> None:  # type: ignore[no-untyped-def]
    with store.lock("m1"), pytest.raises(MatchLockedError):
        make_engine().run(new_state())


def test_flag_never_in_events_or_state_challenges(make_engine, new_state, store_root) -> None:  # type: ignore[no-untyped-def]

    state = make_engine().run(new_state())
    blob = (store_root / "m1.json").read_text()
    for cid in state.challenges:
        assert derive_flag(state.secret, cid) not in blob


def test_invalid_challenges_rejected(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    def build(source: str, payload: str = "x") -> str:
        return json.dumps(
            {"title": "t", "description": "d", "source": source, "reference_payload": payload, "difficulty": 1}
        )

    cases = {
        "trivial": build('import os\nprint(os.environ["FLAG"])\n'),
        "unsolvable": build('print("nope")\n'),
        "escape": build("import socket\n"),
        "hang": build("while True:\n    pass\n"),
    }
    for name, text in cases.items():
        state = make_engine({"alice": Wrapped(FakeProvider(), text=text), "bob": FakeProvider()}).run(
            new_state(f"inv{name}")
        )
        reasons = {v.reason for k, v in state.validations.items() if state.challenges[k].author == "alice"}
        assert all(not state.validations[k].valid for k in state.challenges if state.challenges[k].author == "alice")
        assert reasons, name
