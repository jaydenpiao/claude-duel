"""Timeouts sized for the slowest model in a plan, the same for every model, and a timed-out call ledgers its
reservation (vendors generally bill tokens generated after the client gave up) instead of $0."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app import ladder_config as lc
from claude_duel.app import latency
from claude_duel.app.budget import BudgetLimits, CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.app.ladder import ladder_settings
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError, ProviderTimeoutError
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest
from tests.helpers import any_source

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _load(name: str, over: dict[str, Any] | None = None) -> lc.LadderConfig:
    return lc.load(EXAMPLES / f"ladder-{name}.toml", over, allow_premium=True)


def test_limits_cover_the_slowest_call_seen_for_every_model_in_the_plan() -> None:
    final = _load("final").timeouts()
    slowest = max(latency.latency(m)[1] for m in _load("final").models())
    assert slowest == pytest.approx(781.7)  # gpt-6-astra's slowest logged call stands in for gpt-6.2-astra
    assert final.provider_timeout_s > slowest
    assert final.call_deadline_s >= 2 * final.provider_timeout_s  # one retry after a full timeout
    assert final.match_deadline_s >= latency.MIN_MATCH_DEADLINE_S
    pilot = _load("pilot").timeouts()
    assert (pilot.provider_timeout_s, pilot.call_deadline_s, pilot.match_deadline_s) == (300, 600, 3600)


def test_an_unmeasured_model_gets_the_slowest_measured_size() -> None:
    assert latency.latency("gpt-5-mini") == max(latency.MEASURED.values(), key=lambda r: r[1])


def test_ladder_settings_apply_one_set_of_limits_to_every_model() -> None:
    cfg = _load("pilot")
    settings = ladder_settings(Settings(), cfg)
    assert (settings.provider_timeout, settings.provider_call_deadline) == (300.0, 600.0)
    assert settings.rules.match_deadline_s == 3600
    assert Settings().provider_timeout < settings.provider_timeout  # the 60 s default no longer applies


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"limits.provider_timeout_s": 120}, "below 900s"),
        ({"limits.match_deadline_s": 900}, "below"),
        ({"limits.provider_timeout_s": 2000, "limits.call_deadline_s": 1900}, "call_deadline_s must be"),
    ],
)
def test_limits_too_short_for_the_slowest_model_are_refused(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        _load("final", over)


class TimesOut:
    name = "slow"

    def complete(self, request: CompletionRequest) -> Completion:
        raise ProviderTimeoutError("timeout")


def test_a_timed_out_call_ledgers_its_full_reservation(tmp_path: Path) -> None:
    ledger = FileSpendLedger(tmp_path / "ledger.jsonl")
    budget = CostBudget(BudgetLimits(), ledger, {"priced-1": (1.0, 2.0)})
    engine = MatchEngine(
        {"alice": TimesOut(), "bob": FakeProvider()},
        SubprocessSandbox(any_source),
        FileMatchStore(tmp_path / "m"),
        FileEventLog(tmp_path / "e"),
        InMemoryMetrics(),
        budget,
        {"alice": "priced-1", "bob": "priced-1"},
    )
    agents = (AgentSpec(name="alice", model="priced-1"), AgentSpec(name="bob", model="priced-1"))
    state = MatchState(
        match_id="to-1",
        secret="s" * 64,
        agents=agents,
        rules=Rules(rounds=1, challenges_per_round=1),
        limits=SandboxLimits(),
    )
    with pytest.raises(ProviderTimeoutError):
        engine.run(state)
    spent = ledger.spent_usd("to-1")
    assert spent > 0  # the reserved worst case, not $0
    assert spent == pytest.approx(budget.run_usd)
