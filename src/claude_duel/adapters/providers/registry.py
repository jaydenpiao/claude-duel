"""Provider registry: the one place a real vendor is declared (ADR 0013).

Adding a vendor means one adapter module plus one ``Vendor`` entry here. Config (API key from ``key_env``,
``<name>_model`` and ``DUEL_<NAME>_MODEL``), the container and the budget's vendor check all read this table.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.ports import LLMProvider


@dataclass(frozen=True)
class Vendor:
    name: str
    model_prefix: str  # every allowlisted model of this vendor starts with it
    key_env: str  # the API key is read from this env var only, never from files
    default_model: str
    factory: Callable[[SecretStr, str, float], LLMProvider]  # (api key, model, timeout) -> adapter


VENDORS: dict[str, Vendor] = {
    v.name: v
    for v in (
        Vendor("openai", "gpt-", "OPENAI_API_KEY", "gpt-5-nano", lambda k, m, t: OpenAIProvider(k, m, timeout=t)),
        Vendor(
            "anthropic",
            "claude-",
            "ANTHROPIC_API_KEY",
            "claude-haiku-5-5",
            lambda k, m, t: AnthropicProvider(k, m, timeout=t),
        ),
    )
}
