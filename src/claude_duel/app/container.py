"""Composition root: the only place adapters are chosen."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.event_log import FileEventLog, ObservedEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import CostBudget, check_model_allowed
from claude_duel.app.engine import MatchEngine
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec
from claude_duel.observability import log
from claude_duel.ports import EventObserver, LLMProvider, Metrics, Sandbox

_LOG = logging.getLogger(__name__)


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
    docker = DockerSandbox(settings.docker_command, settings.docker_image)
    problem = docker.health()
    if problem is None:
        return docker
    if settings.sandbox == "docker" or untrusted:
        raise ConfigError(f"docker sandbox required but unavailable: {problem}")
    log(_LOG, logging.WARNING, "docker unavailable; using subprocess sandbox for a fake-only match", reason=problem)
    return SubprocessSandbox()


def build_store(settings: Settings) -> FileMatchStore:
    return FileMatchStore(settings.store_dir)


def build_event_log(settings: Settings) -> FileEventLog:
    return FileEventLog(settings.store_dir)


def build_ledger(settings: Settings) -> FileSpendLedger:
    return FileSpendLedger(settings.store_dir / "ledger.jsonl")


def pin_agents(settings: Settings) -> tuple[AgentSpec, AgentSpec]:
    """Agents with their effective model written in, so a saved match never depends on later config."""
    a, b = (agent.model_copy(update={"model": settings.model_for(agent)}) for agent in settings.agents)
    return a, b


def build_engine(
    settings: Settings,
    metrics: Metrics,
    store: FileMatchStore,
    agents: tuple[AgentSpec, AgentSpec],
    observer: EventObserver | None = None,
) -> MatchEngine:
    """Wire an engine for a match's own (saved) agents; settings only supply keys, adapters and caps."""
    for agent in agents:
        check_model_allowed(settings.model_for(agent), settings.prices)
    return MatchEngine(
        {a.name: make_provider(a, settings, metrics) for a in agents},
        make_sandbox(settings, agents),
        store,
        ObservedEventLog(build_event_log(settings), observer) if observer else build_event_log(settings),
        metrics,
        CostBudget(settings.budget, build_ledger(settings), settings.prices),
        {a.name: settings.model_for(a) for a in agents},
    )
