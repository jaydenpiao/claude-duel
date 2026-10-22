from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.docker_sandbox import docker_probe
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import LLMProvider

AGENTS = (AgentSpec(name="alice", skill=2), AgentSpec(name="bob", skill=5))


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def store(store_root: Path) -> FileMatchStore:
    return FileMatchStore(store_root)


@pytest.fixture
def new_state() -> Callable[..., MatchState]:
    def make(match_id: str = "m1", **rules: int) -> MatchState:
        return MatchState(
            match_id=match_id,
            secret="s" * 64,
            agents=AGENTS,
            rules=Rules(rounds=2, challenges_per_round=2, **rules),
            limits=SandboxLimits(),
        )

    return make


@pytest.fixture
def make_engine(store: FileMatchStore, store_root: Path) -> Callable[..., MatchEngine]:
    def make(
        providers: dict[str, LLMProvider] | None = None,
        metrics: InMemoryMetrics | None = None,
        budget: BudgetLimits | None = None,
    ) -> MatchEngine:
        providers = providers or {"alice": FakeProvider(skill=2), "bob": FakeProvider(skill=5)}
        return MatchEngine(
            providers,
            SubprocessSandbox(),
            store,
            metrics or InMemoryMetrics(),
            CostBudget(budget or BudgetLimits(), FileSpendLedger(store_root / "ledger.jsonl"), DEFAULT_PRICES),
            {"alice": "fake-1", "bob": "fake-1"},
        )

    return make


@pytest.fixture(scope="session")
def docker_cmd() -> tuple[str, ...]:
    """A working docker CLI (DUEL_DOCKER, plain docker, or passwordless sudo); skips the test otherwise."""
    candidates = [tuple(shlex.split(os.environ["DUEL_DOCKER"]))] if os.environ.get("DUEL_DOCKER") else []
    candidates += [("docker",), ("sudo", "-n", "docker")]
    for cmd in candidates:
        if docker_probe(cmd) is None:
            return cmd
    pytest.skip("docker daemon not reachable")
