"""S17-1 regression (security review 2026-11-26): a timed-out try is likely billed, so it is ledgered at its
reservation. Timeouts are retried by the engine, not in RetryingProvider, so each try has its own reservation."""

from __future__ import annotations

import json

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.app.engine import TIMEOUT_RETRIES
from claude_duel.domain.errors import ProviderTimeoutError
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest


class Stalls:
    name = "fake"

    def __init__(self, stalls: int) -> None:
        self.fake, self.stalls, self.tries = FakeProvider(skill=3), stalls, 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.tries += 1
        if self.stalls > 0:
            self.stalls -= 1
            raise ProviderTimeoutError("openai timeout")  # the vendor kept generating (and billing) after this
        return self.fake.complete(request)


def _ledger(store_root):  # type: ignore[no-untyped-def]
    return [json.loads(x) for x in (store_root / "ledger.jsonl").read_text().splitlines() if x.strip()]


def _state(mid: str) -> MatchState:
    agents = (AgentSpec(name="alice", model="fake-1"), AgentSpec(name="bob", model="fake-1"))
    return MatchState(
        match_id=mid,
        secret="s" * 64,
        agents=agents,
        rules=Rules(rounds=1, challenges_per_round=1),
        limits=SandboxLimits(),
    )


def test_every_timed_out_try_is_ledgered_at_its_reservation(make_engine, store_root) -> None:  # type: ignore[no-untyped-def]
    inner = Stalls(stalls=TIMEOUT_RETRIES)
    alice = RetryingProvider(inner, InMemoryMetrics(), max_retries=4, sleep=lambda s: None)
    make_engine({"alice": alice, "bob": FakeProvider(skill=3)}).run(_state("to-1"))
    rows = _ledger(store_root)
    first = rows[: 2 * (TIMEOUT_RETRIES + 1)]  # alice's first call: one reserve + one settle per vendor try
    reserves = [r for r in first if "reserve" in r]
    settles = {r["settles"]: r for r in first if "settles" in r}
    assert inner.tries >= TIMEOUT_RETRIES + 1
    assert len(reserves) == TIMEOUT_RETRIES + 1
    assert len(settles) == TIMEOUT_RETRIES + 1
    kept = [settles[r["reserve"]] for r in reserves[:TIMEOUT_RETRIES]]
    assert all(k["tokens"] > 0 for k in kept)  # settled at the reserved worst case, not released at 0


def test_timeouts_past_the_retries_fail_the_call(make_engine, store_root) -> None:  # type: ignore[no-untyped-def]
    inner = Stalls(stalls=99)
    alice = RetryingProvider(inner, InMemoryMetrics(), max_retries=4, sleep=lambda s: None)
    with pytest.raises(ProviderTimeoutError):
        make_engine({"alice": alice, "bob": FakeProvider(skill=3)}).run(_state("to-2"))
    assert inner.tries == TIMEOUT_RETRIES + 1  # the provider no longer retries timeouts itself
    rows = _ledger(store_root)
    assert sum("reserve" in r for r in rows) == sum("settles" in r and r["tokens"] > 0 for r in rows) == inner.tries


def test_retrying_provider_does_not_retry_timeouts() -> None:
    inner = Stalls(stalls=1)
    with pytest.raises(ProviderTimeoutError):
        RetryingProvider(inner, InMemoryMetrics(), sleep=lambda s: None).complete(
            CompletionRequest(system="s", prompt="p", purpose="build")
        )
    assert inner.tries == 1
