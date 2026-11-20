from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from hypothesis import settings

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.docker_sandbox import docker_probe
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import EventObserver, LLMProvider
from tests.helpers import any_source

# No per-example deadline: a loaded CI box made property tests flaky (DeadlineExceeded), not slow code.
settings.register_profile("duel", deadline=None)
settings.load_profile("duel")

AGENTS = (AgentSpec(name="alice", skill=2), AgentSpec(name="bob", skill=5))


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def store(store_root: Path) -> FileMatchStore:
    return FileMatchStore(store_root)


@pytest.fixture
def event_log(store_root: Path) -> FileEventLog:
    return FileEventLog(store_root)


@pytest.fixture
def new_state() -> Callable[..., MatchState]:
    def make(match_id: str = "m1", **rules: int) -> MatchState:
        return MatchState(
            match_id=match_id,
            secret="s" * 64,
            agents=AGENTS,
            rules=Rules(**{"rounds": 2, "challenges_per_round": 2, **rules}),
            limits=SandboxLimits(),
        )

    return make


@pytest.fixture
def make_engine(store: FileMatchStore, event_log: FileEventLog, store_root: Path) -> Callable[..., MatchEngine]:
    def make(
        providers: dict[str, LLMProvider] | None = None,
        metrics: InMemoryMetrics | None = None,
        budget: BudgetLimits | None = None,
        observer: EventObserver | None = None,
    ) -> MatchEngine:
        providers = providers or {"alice": FakeProvider(skill=2), "bob": FakeProvider(skill=5)}
        return MatchEngine(
            providers,
            SubprocessSandbox(any_source),
            store,
            event_log,
            metrics or InMemoryMetrics(),
            CostBudget(budget or BudgetLimits(), FileSpendLedger(store_root / "ledger.jsonl"), DEFAULT_PRICES),
            dict.fromkeys(providers, "fake-1"),
            observer,
        )

    return make


def find_docker(env: Mapping[str, str]) -> tuple[tuple[str, ...] | None, list[str]]:
    """The first working docker CLI (DUEL_DOCKER, plain docker, or passwordless sudo), plus each refusal reason."""
    candidates = [tuple(shlex.split(env["DUEL_DOCKER"]))] if env.get("DUEL_DOCKER") else []
    candidates += [("docker",), ("sudo", "-n", "docker")]
    reasons = []
    for cmd in candidates:
        problem = docker_probe(cmd)
        if problem is None:
            return cmd, reasons
        reasons.append(f"{' '.join(cmd)}: {problem}")
    return None, reasons


@pytest.fixture(scope="session")
def docker_cmd() -> tuple[str, ...]:
    """A working docker CLI; skips the test otherwise, or fails it when DUEL_REQUIRE_DOCKER=1 (CI)."""
    cmd, reasons = find_docker(os.environ)
    if cmd is not None:
        return cmd
    if os.environ.get("DUEL_REQUIRE_DOCKER") == "1":
        pytest.fail("DUEL_REQUIRE_DOCKER=1 but no usable docker: " + "; ".join(reasons), pytrace=False)
    pytest.skip("docker daemon not reachable")


_REAL_DB_VARS = ("DATABASE_URL", "MIGRATE_DATABASE_URL", "EXPORT_DATABASE_URL")


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real results-db URLs must never reach a test; tests use DUEL_TEST_DATABASE_URL."""
    for var in _REAL_DB_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolated_install_id(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the per-install OpenAI safety identifier out of the developer's real store dir."""
    path = tmp_path_factory.mktemp("install") / "install-id"
    monkeypatch.setattr("claude_duel.adapters.providers.openai.install_id_path", lambda store_dir=None: path)
