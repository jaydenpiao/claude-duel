"""Prereg control 1: model names are display labels only; prompts name the agents `a`/`b`."""

from __future__ import annotations

from pathlib import Path

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest
from tests.helpers import any_source

MODELS = {"a": "claude-haiku-5-5", "b": "gpt-5-mini"}
VENDOR_WORDS = ("claude", "anthropic", "haiku", "gpt", "openai", *MODELS.values())


class Recorder:
    name = "recorder"

    def __init__(self, skill: int) -> None:
        self.fake, self.texts = FakeProvider(skill=skill), []  # type: list[str]

    def complete(self, request: CompletionRequest) -> Completion:
        self.texts.append(f"{request.system}\n{request.prompt}")
        return self.fake.complete(request)


def test_prompts_never_name_the_model_or_vendor(
    store: FileMatchStore, event_log: FileEventLog, store_root: Path
) -> None:
    rec = {"a": Recorder(2), "b": Recorder(5)}
    engine = MatchEngine(
        dict(rec),
        SubprocessSandbox(any_source),
        store,
        event_log,
        InMemoryMetrics(),
        CostBudget(BudgetLimits(), FileSpendLedger(store_root / "ledger.jsonl"), DEFAULT_PRICES),
        MODELS,
    )
    agents = tuple(AgentSpec(name=n, skill=2, model=m) for n, m in MODELS.items())
    state = MatchState(match_id="m1", secret="s" * 64, agents=agents, rules=Rules(rounds=2), limits=SandboxLimits())
    engine.run(state)
    texts = [t.lower() for r in rec.values() for t in r.texts]
    assert texts
    for word in VENDOR_WORDS:
        assert not any(word in t for t in texts), word
