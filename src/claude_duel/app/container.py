"""Composition root: the only place adapters are chosen."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox, docker_available
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
        model = model_for(agent, settings)
        inner = OpenAIProvider(settings.openai_api_key, model, timeout=settings.provider_timeout)
        return RetryingProvider(inner, metrics, max_retries=settings.provider_max_retries)
    raise ConfigError(f"unknown provider {agent.provider!r}")


def model_for(agent: AgentSpec, settings: Settings) -> str:
    if agent.provider == "openai" and agent.model == "fake-1":
        return settings.openai_model
    return agent.model


def make_sandbox(kind: str) -> Sandbox:
    if kind == "docker" or (kind == "auto" and docker_available()):
        return DockerSandbox()
    return SubprocessSandbox()


def build_engine(settings: Settings, metrics: Metrics) -> tuple[MatchEngine, FileMatchStore]:
    store = FileMatchStore(settings.store_dir)
    providers = {a.name: make_provider(a, settings, metrics) for a in settings.agents}
    models = {a.name: model_for(a, settings) for a in settings.agents}
    engine = MatchEngine(
        providers,
        make_sandbox(settings.sandbox),
        store,
        metrics,
        CostBudget(settings.budget, FileSpendLedger(settings.store_dir / "ledger.jsonl"), settings.prices),
        models,
    )
    return engine, store
