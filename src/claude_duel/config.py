"""Configuration: TOML file, then DUEL_* env overrides. Secrets only from env."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec, Rules, SandboxLimits


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: tuple[AgentSpec, AgentSpec] = (AgentSpec(name="alice", skill=2), AgentSpec(name="bob", skill=3))
    rules: Rules = Rules()
    limits: SandboxLimits = SandboxLimits()
    sandbox: str = Field(default="auto", pattern="^(auto|docker|subprocess)$")
    store_dir: Path = Path(".duel")
    log_level: str = "INFO"
    openai_model: str = "gpt-4o-mini"
    provider_timeout: float = 60.0
    provider_max_retries: int = 4
    openai_api_key: SecretStr | None = None


_ENV_SCALARS = {
    "DUEL_SANDBOX": "sandbox",
    "DUEL_STORE_DIR": "store_dir",
    "DUEL_LOG_LEVEL": "log_level",
    "DUEL_OPENAI_MODEL": "openai_model",
    "DUEL_PROVIDER_TIMEOUT": "provider_timeout",
}
_ENV_RULES = {
    "DUEL_ROUNDS": "rounds",
    "DUEL_MAX_ATTEMPTS": "max_attempts",
    "DUEL_CHALLENGES_PER_ROUND": "challenges_per_round",
    "DUEL_TOKEN_BUDGET": "attack_token_budget",
}


def load_settings(path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    data: dict[str, Any] = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
    for var, key in _ENV_SCALARS.items():
        if var in env:
            data[key] = env[var]
    rules = dict(data.get("rules", {}))
    for var, key in _ENV_RULES.items():
        if var in env:
            rules[key] = env[var]
    if rules:
        data["rules"] = rules
    if env.get("OPENAI_API_KEY"):
        data["openai_api_key"] = env["OPENAI_API_KEY"]
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
