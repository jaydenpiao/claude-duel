"""Provider factories, keyed by vendor name (ADR 0013).

Vendor metadata (prefix, key env, default model) lives in ``domain/vendors.VENDORS``; only the composition
root (``app/container.py``) reads this map. Adding a vendor means one adapter module, one ``Vendor`` entry
and one factory here.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import SecretStr

from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.ports import LLMProvider

Factory = Callable[[SecretStr, str, float], LLMProvider]  # (api key, model, timeout) -> adapter

FACTORIES: dict[str, Factory] = {
    "openai": lambda k, m, t: OpenAIProvider(k, m, timeout=t),
    "anthropic": lambda k, m, t: AnthropicProvider(k, m, timeout=t),
}
