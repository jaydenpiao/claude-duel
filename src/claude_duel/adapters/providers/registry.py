"""Provider factories, keyed by vendor name (ADR 0013).

Vendor metadata (prefix, key env, default model) lives in ``domain/vendors.VENDORS``; only the composition
root (``app/container.py``) reads this map. Adding a vendor means one adapter module, one ``Vendor`` entry
and one factory here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import SecretStr

from claude_duel.adapters.providers import openai as openai_provider
from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.openai import OpenAIProvider, default_safety_identifier
from claude_duel.ports import LLMProvider

Factory = Callable[[SecretStr, str, float, Path], LLMProvider]  # (api key, model, timeout, store dir) -> adapter

FACTORIES: dict[str, Factory] = {
    "openai": lambda k, m, t, d: OpenAIProvider(
        k, m, timeout=t, safety_identifier=default_safety_identifier(openai_provider.install_id_path(d))
    ),
    "anthropic": lambda k, m, t, d: AnthropicProvider(k, m, timeout=t),
}
