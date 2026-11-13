"""Vendor metadata: the one place a real vendor is declared (ADR 0013).

Plain data with no adapter imports, so config and the budget's vendor check stay free of HTTP clients
(quality M11). The adapter for each vendor is in ``adapters/providers/registry.FACTORIES``, keyed by name.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Vendor:
    name: str
    model_prefix: str  # every allowlisted model of this vendor starts with it
    key_env: str  # the API key is read from this env var only, never from files
    default_model: str


VENDORS: dict[str, Vendor] = {
    v.name: v
    for v in (
        Vendor("openai", "gpt-", "OPENAI_API_KEY", "gpt-6-luna"),
        Vendor("anthropic", "claude-", "ANTHROPIC_API_KEY", "claude-haiku-5-5"),
    )
}
