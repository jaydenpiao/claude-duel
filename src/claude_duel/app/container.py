"""Composition root: the only place adapters are chosen."""

from __future__ import annotations

from collections.abc import Iterable

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox, docker_probe
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec
from claude_duel.ports import LLMProvider, Metrics, Sandbox


def make_provider(agent: AgentSpec, settings: Settings, metrics: Metrics) -> LLMProvider:
    if agent.provider == "fake":
        return FakeProvider(skill=agent.skill)
    if agent.provider == "openai":
        if settings.openai_api_key is None:
            raise ConfigError("agent uses openai but OPENAI_API_KEY is not set")
        inner = OpenAIProvider(settings.openai_api_key, settings.model_for(agent), timeout=settings.provider_timeout)
        return RetryingProvider(inner, metrics, max_retries=settings.provider_max_retries)
    raise ConfigError(f"unknown provider {agent.provider!r}")


def make_sandbox(settings: Settings, agents: Iterable[AgentSpec]) -> Sandbox:
    """Real-model matches only ever run in Docker; fail closed when it isn't usable (ADR 0003)."""
    untrusted = any(a.provider != "fake" for a in agents)
    if settings.sandbox == "subprocess":
        if untrusted:
            raise ConfigError("real-model matches require the docker sandbox; subprocess is for the fake provider")
        return SubprocessSandbox()
    problem = docker_probe(settings.docker_command)
    if problem is None:
        return DockerSandbox(settings.docker_command, settings.docker_image)
    if settings.sandbox == "docker" or untrusted:
        raise ConfigError(f"docker sandbox required but unavailable: {problem}")
    return SubprocessSandbox()


def build_store(settings: Settings) -> FileMatchStore:
    return FileMatchStore(settings.store_dir)


def build_engine(settings: Settings, metrics: Metrics, store: FileMatchStore) -> MatchEngine:
    agents = settings.agents
    return MatchEngine(
        {a.name: make_provider(a, settings, metrics) for a in agents},
        make_sandbox(settings, agents),
        store,
        metrics,
        CostBudget(settings.budget, FileSpendLedger(settings.store_dir / "ledger.jsonl"), settings.prices),
        {a.name: settings.model_for(a) for a in agents},
    )
