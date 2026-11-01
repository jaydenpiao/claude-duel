"""Composition root: the only place adapters are chosen."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from pydantic import SecretStr

from claude_duel.adapters.metrics.jsonl_metrics import JsonlMetrics, TeeMetrics
from claude_duel.adapters.providers.fake import FakeProvider, is_template
from claude_duel.adapters.providers.registry import FACTORIES
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox, docker_runtimes
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileLock, FileMatchStore
from claude_duel.adapters.store.gcs import GcloudUploader
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.adapters.store.results_sql import (
    Executor,
    SqlResults,
    migrate,
    refuse_superuser,
    refuse_table_owner,
    require_current,
)
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget, RunOverride, check_model_allowed
from claude_duel.app.engine import GATE, MatchEngine
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec
from claude_duel.domain.vendors import VENDORS
from claude_duel.observability import log
from claude_duel.ports import EventObserver, LLMProvider, Metrics, PublicSource, ResultsStore, Sandbox, Uploader

_LOG = logging.getLogger(__name__)


def make_provider(agent: AgentSpec, settings: Settings, metrics: Metrics) -> LLMProvider:
    if agent.provider == "fake":
        return FakeProvider(skill=agent.skill)
    model, timeout = settings.model_for(agent), settings.provider_timeout
    vendor = VENDORS.get(agent.provider)
    if vendor is None:
        raise ConfigError(f"unknown provider {agent.provider!r}")
    key = settings.api_keys.get(vendor.name)
    if key is None:
        raise ConfigError(f"agent uses {vendor.name} but {vendor.key_env} is not set")
    inner = FACTORIES[vendor.name](key, model, timeout)
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


def build_uploader() -> Uploader:
    return GcloudUploader()


def build_event_log(settings: Settings) -> FileEventLog:
    return FileEventLog(settings.store_dir)


def build_driver_lock(settings: Settings, log_id: str, kind: str) -> FileLock:
    """Non-blocking lock ``<id>.<kind>.lock`` held by a series/tournament driver for its whole run (#120)."""
    settings.store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return FileLock(settings.store_dir / f"{log_id}.{kind}.lock")


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


def build_budget(settings: Settings, override: RunOverride | None = None) -> CostBudget:
    """One per process invocation: its run counters are what ``run_max_calls``/``run_max_usd`` cap.

    ``override`` (``duel experiment`` only) is the one place a cap is raised: a copy of the limits, so the
    configured defaults and every other ``CostBudget`` are untouched."""
    limits = settings.budget
    if override is not None:
        # model_validate, not model_copy: the field validators must run on the raised value
        limits = BudgetLimits.model_validate({**limits.model_dump(), override.field: override.value})
    return CostBudget(limits, build_ledger(settings), DEFAULT_PRICES, override)


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
    """Tournament entrants with their effective model written in (#125), like ``pin_agents``."""
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


def _db_url(secret: SecretStr | None, var: str) -> str:
    if secret is None:
        raise ConfigError(f"{var} is not set")
    return secret.get_secret_value()


def _owner_url(settings: Settings) -> str:
    if settings.migrate_database_url is not None:
        return _db_url(settings.migrate_database_url, "MIGRATE_DATABASE_URL")
    return _db_url(settings.database_url, "MIGRATE_DATABASE_URL (or DATABASE_URL)")


def results_executor(url: str) -> Executor:
    """psycopg to the results database (in production a cloud-sql-proxy Unix socket in a 0700 dir, ADR 0012).

    psycopg is imported only when used.
    """
    from claude_duel.adapters.store.postgres import PgExecutor, connect  # noqa: PLC0415 - psycopg only when used

    return PgExecutor(connect(url))


def build_results(settings: Settings, *, owner: bool = False) -> ResultsStore:
    """Connect with ``DATABASE_URL`` (or the owner's ``MIGRATE_DATABASE_URL`` for ``--rebuild``); never migrates.

    Fails clearly if migrations are pending: run ``duel db migrate``.
    """
    ex = results_executor(_owner_url(settings) if owner else _db_url(settings.database_url, "DATABASE_URL"))
    refuse_superuser(ex)
    require_current(ex)
    if not owner:
        refuse_table_owner(ex)
    return SqlResults(ex)


def migrate_results(settings: Settings) -> list[str]:
    """``duel db migrate``: apply pending migrations as the schema owner. Returns the names applied."""
    return migrate(results_executor(_owner_url(settings)))


def git_sha() -> str | None:
    """The commit ``duel sync`` runs from (recorded per run), or None outside a checkout."""
    if sha := os.environ.get("GITHUB_SHA"):
        return sha
    try:
        out = subprocess.run(  # fixed argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def build_public_reader(settings: Settings) -> PublicSource:
    """Connect with ``EXPORT_DATABASE_URL``; ``PublicReader`` refuses any role that could write (postgres only)."""
    from claude_duel.adapters.store.postgres import PublicReader, connect  # noqa: PLC0415 - psycopg only when used

    return PublicReader(connect(_db_url(settings.export_database_url, "EXPORT_DATABASE_URL")))
