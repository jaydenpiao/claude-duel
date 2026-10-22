"""Configuration: TOML file, then DUEL_* env overrides. Secrets only from env."""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from claude_duel.app.budget import FAKE_MODEL, BudgetLimits, check_model_allowed
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec, Rules, SandboxLimits
from claude_duel.domain.modes import preset
from claude_duel.domain.tournament import TournamentRule


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: tuple[AgentSpec, AgentSpec] = (AgentSpec(name="alice", skill=2), AgentSpec(name="bob", skill=3))
    rules: Rules = Rules()
    limits: SandboxLimits = SandboxLimits()
    sandbox: str = Field(default="auto", pattern="^(auto|docker|subprocess)$")
    store_dir: Path = Path(".duel")
    log_level: str = "INFO"
    openai_model: str = "gpt-4.1-nano"
    seed: str | None = None  # match seed; default sha256(match_id)[:16]
    gate_model: str = "gpt-4.1-nano"  # the cheapest low-tier model; used only when rules.difficulty_gate
    budget: BudgetLimits = BudgetLimits()
    tournament: TournamentRule | None = None  # [tournament] entrants for `duel tournament` (#39)
    provider_timeout: float = 60.0
    provider_max_retries: int = 4
    provider_call_deadline: float = Field(default=120.0, gt=0)  # seconds per call, retries included
    openai_api_key: SecretStr | None = None
    database_url: SecretStr | None = None  # DATABASE_URL: results DB owner, for `duel sync` (ADR 0012)
    export_database_url: SecretStr | None = None  # EXPORT_DATABASE_URL: a duel_exporter login, for `duel export`
    docker_command: tuple[str, ...] = ("docker",)
    docker_image: str = "python:3.12-slim"
    metrics: str = Field(default="log", pattern="^(log|jsonl)$")  # jsonl: also append to <store>/metrics.jsonl
    docker_runtime: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]*$")  # e.g. "runsc" (ADR 0011)

    def model_for(self, agent: AgentSpec) -> str:
        """The single place an agent's effective model is resolved."""
        if agent.provider == "fake":
            return FAKE_MODEL
        return agent.model or self.openai_model


_ENV_SCALARS = {
    "DUEL_SANDBOX": "sandbox",
    "DUEL_METRICS": "metrics",
    "DUEL_DOCKER_RUNTIME": "docker_runtime",
    "DUEL_STORE_DIR": "store_dir",
    "DUEL_LOG_LEVEL": "log_level",
    "DUEL_OPENAI_MODEL": "openai_model",
    "DUEL_GATE_MODEL": "gate_model",
    "DUEL_PROVIDER_TIMEOUT": "provider_timeout",
    "DUEL_PROVIDER_MAX_RETRIES": "provider_max_retries",
}
_ENV_RULES = {
    "DUEL_ROUNDS": "rounds",
    "DUEL_MAX_ATTEMPTS": "max_attempts",
    "DUEL_CHALLENGES_PER_ROUND": "challenges_per_round",
    "DUEL_MODE": "mode",
    "DUEL_TOKEN_BUDGET": "attack_token_budget",
}

_ENV_BUDGET = {
    "DUEL_MATCH_MAX_USD": "match_max_usd",
    "DUEL_RUN_MAX_USD": "run_max_usd",
    "DUEL_MATCH_MAX_CALLS": "match_max_calls",
    "DUEL_MATCH_MAX_TOKENS": "match_max_tokens",
    "DUEL_RUN_MAX_CALLS": "run_max_calls",
    "DUEL_LIFETIME_MAX_USD": "lifetime_max_usd",
}
_ENV_SECRETS = {
    "OPENAI_API_KEY": "openai_api_key",
    "DATABASE_URL": "database_url",
    "EXPORT_DATABASE_URL": "export_database_url",
}
_ENV_SECTIONS = {"rules": _ENV_RULES, "budget": _ENV_BUDGET}
ENV_VARS = (*_ENV_SCALARS, *_ENV_RULES, *_ENV_BUDGET, "DUEL_DOCKER")  # every DUEL_* knob; README must list all


def _overlay(target: dict[str, Any], mapping: dict[str, str], env: dict[str, str]) -> None:
    for var, key in mapping.items():
        if var in env:
            target[key] = env[var]


def _with_preset(rules: dict[str, Any]) -> dict[str, Any]:
    """Preset first, then the config's own fields (shallow: a given section such as ``scoring`` replaces it)."""
    name = str(rules.get("mode", "classic"))
    try:
        base = preset(name)
    except KeyError as exc:
        raise ConfigError(str(exc.args[0])) from exc
    return base.model_dump() | rules


def _read_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc


def load_settings(path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    data = _read_toml(path)
    _overlay(data, _ENV_SCALARS, env)
    for section, mapping in _ENV_SECTIONS.items():
        nested = dict(data.get(section, {}))
        _overlay(nested, mapping, env)
        if nested:
            data[section] = nested
    if "rules" in data:
        data["rules"] = _with_preset(data["rules"])
    if env.get("DUEL_DOCKER"):
        data["docker_command"] = shlex.split(env["DUEL_DOCKER"])
    for var, key in _ENV_SECRETS.items():
        if env.get(var):
            data[key] = env[var]
    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    check_model_allowed(settings.openai_model)
    for agent in settings.agents:
        check_model_allowed(settings.model_for(agent))
    return settings
