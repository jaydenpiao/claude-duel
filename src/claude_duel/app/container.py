"""Composition root: the only place adapters are chosen."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from claude_duel.adapters.metrics.jsonl_metrics import JsonlMetrics, TeeMetrics
from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.fake import FakeProvider, is_template
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox, docker_runtimes
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, CostBudget, check_model_allowed
from claude_duel.app.engine import GATE, MatchEngine
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec
from claude_duel.observability import log
from claude_duel.ports import EventObserver, LLMProvider, Metrics, Sandbox

_LOG = logging.getLogger(__name__)


def make_provider(agent: AgentSpec, settings: Settings, metrics: Metrics) -> LLMProvider:
    if agent.provider == "fake":
        return FakeProvider(skill=agent.skill)
    model, timeout = settings.model_for(agent), settings.provider_timeout
    inner: LLMProvider
    if agent.provider == "openai":
        if settings.openai_api_key is None:
            raise ConfigError("agent uses openai but OPENAI_API_KEY is not set")
        inner = OpenAIProvider(settings.openai_api_key, model, timeout=timeout)
    elif agent.provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise ConfigError("agent uses anthropic but ANTHROPIC_API_KEY is not set")
        inner = AnthropicProvider(settings.anthropic_api_key, model, timeout=timeout)
    else:
        raise ConfigError(f"unknown provider {agent.provider!r}")
    return RetryingProvider(
        inner, metrics, max_retries=settings.provider_max_retries, deadline_s=settings.provider_call_deadline
    )


def docker_runtime(settings: Settings) -> str | None:
    """The requested runtime if the daemon has it; otherwise warn and fall back to the default (ADR 0011)."""
    wanted = settings.docker_runtime
    if wanted is None or wanted in docker_runtimes(settings.docker_command):
        return wanted
    log(_LOG, logging.WARNING, "docker runtime not registered; using the daemon default", runtime=wanted)
    return None


def make_sandbox(settings: Settings, agents: Iterable[AgentSpec]) -> Sandbox:
    """Real-model matches only ever run in Docker; fail closed when it isn't usable (ADR 0003)."""
    untrusted = any(a.provider != "fake" for a in agents)
    if settings.sandbox == "subprocess":
        if untrusted:
            raise ConfigError("real-model matches require the docker sandbox; subprocess is for the fake provider")
        return SubprocessSandbox(is_template)
    docker = DockerSandbox(settings.docker_command, settings.docker_image, runtime=docker_runtime(settings))
    problem = docker.health()
    if problem is None:
        return docker
    if settings.sandbox == "docker" or untrusted:
        raise ConfigError(f"docker sandbox required but unavailable: {problem}")
    log(_LOG, logging.WARNING, "docker unavailable; using subprocess sandbox for a fake-only match", reason=problem)
    return SubprocessSandbox(is_template)


def build_store(settings: Settings) -> FileMatchStore:
    return FileMatchStore(settings.store_dir)


def build_event_log(settings: Settings) -> FileEventLog:
    return FileEventLog(settings.store_dir)


def build_series_log(settings: Settings) -> FileEventLog:
    """Series logs (#38) are ordinary hash-chained event logs named ``<series_id>.series.jsonl``."""
    return FileEventLog(settings.store_dir, suffix=".series.jsonl")


def build_metrics(settings: Settings, match_id: str, summary: Metrics) -> Metrics:
    """The in-memory summary, plus the JSON-lines sink when ``metrics = "jsonl"`` (#11)."""
    if settings.metrics == "log":
        return summary
    return TeeMetrics((summary, JsonlMetrics(settings.store_dir / "metrics.jsonl", match_id)))


def build_tournament_log(settings: Settings) -> FileEventLog:
    """Tournament logs (#39), ``<tournament_id>.tournament.jsonl``, same hash chain."""
    return FileEventLog(settings.store_dir, suffix=".tournament.jsonl")


def build_ledger(settings: Settings) -> FileSpendLedger:
    return FileSpendLedger(settings.store_dir / "ledger.jsonl")


def build_budget(settings: Settings) -> CostBudget:
    """One per process invocation: its run counters are what ``run_max_calls``/``run_max_usd`` cap."""
    return CostBudget(settings.budget, build_ledger(settings), DEFAULT_PRICES)


def gate_agent(settings: Settings, agents: tuple[AgentSpec, AgentSpec]) -> AgentSpec:
    """The difficulty-gate probe: the cheap gate model when the match uses real models, else a weak fake."""
    if all(a.provider == "fake" for a in agents):
        return AgentSpec(name=GATE, provider="fake", skill=0)
    return AgentSpec(name=GATE, provider="openai", model=settings.gate_model)


def pin_agents(settings: Settings) -> tuple[AgentSpec, AgentSpec]:
    """Agents with their effective model written in, so a saved match never depends on later config."""
    a, b = (agent.model_copy(update={"model": settings.model_for(agent)}) for agent in settings.agents)
    return a, b


def pin_entrants(settings: Settings, entrants: tuple[AgentSpec, ...]) -> tuple[AgentSpec, ...]:
    """Tournament entrants with their effective model written in (arch r3 #15), like ``pin_agents``."""
    return tuple(e.model_copy(update={"model": settings.model_for(e)}) for e in entrants)


def build_engine(
    settings: Settings,
    metrics: Metrics,
    store: FileMatchStore,
    agents: tuple[AgentSpec, AgentSpec],
    observer: EventObserver | None = None,
    budget: CostBudget | None = None,
) -> MatchEngine:
    """Wire an engine for a match's own (saved) agents; settings only supply keys, adapters and caps.

    ``budget`` is the invocation's shared ``CostBudget``: a series or tournament passes one so
    ``run_max_*`` spans every game it plays; omitted, the engine gets a fresh one (a single match)."""
    for agent in agents:
        check_model_allowed(settings.model_for(agent), settings.allow_premium)
    providers = {a.name: make_provider(a, settings, metrics) for a in agents}
    models = {a.name: settings.model_for(a) for a in agents}
    if settings.rules.difficulty_gate:
        probe = gate_agent(settings, agents)
        check_model_allowed(settings.model_for(probe))
        providers[GATE], models[GATE] = make_provider(probe, settings, metrics), settings.model_for(probe)
    return MatchEngine(
        providers,
        make_sandbox(settings, agents),
        store,
        build_event_log(settings),
        metrics,
        budget or build_budget(settings),
        models,
        observer,
    )
