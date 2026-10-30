"""Provider registry and shared HTTP error mapping (ADR 0013)."""

from __future__ import annotations

import httpx
import pytest

from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.http import raise_for_status
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.adapters.providers.registry import VENDORS
from claude_duel.app.budget import DEFAULT_PRICES, FAKE_MODEL, vendor_of
from claude_duel.app.container import make_provider
from claude_duel.config import load_settings
from claude_duel.domain.errors import (
    ConfigError,
    PermanentProviderError,
    QuotaExceededError,
    RateLimitError,
    TransientProviderError,
)
from claude_duel.domain.models import AgentSpec
from claude_duel.observability import InMemoryMetrics


def _resp(status: int, text: str = "", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers or {})


@pytest.mark.parametrize(
    ("status", "text", "error"),
    [
        (429, '{"error":{"code":"insufficient_quota"}}', QuotaExceededError),
        (429, "slow down", RateLimitError),
        (500, "", TransientProviderError),
        (408, "", TransientProviderError),
        (400, "", PermanentProviderError),
    ],
)
def test_raise_for_status_maps_errors(status: int, text: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        raise_for_status(_resp(status, text), "acme")


def test_raise_for_status_passes_success_and_names_vendor_on_quota() -> None:
    raise_for_status(_resp(200), "acme")
    with pytest.raises(QuotaExceededError, match="acme"):
        raise_for_status(_resp(429, "insufficient_quota"), "acme")


@pytest.mark.parametrize(("value", "expected"), [("2.5", 2.5), ("soon", None), (None, None)])
def test_retry_after_header(value: str | None, expected: float | None) -> None:
    with pytest.raises(RateLimitError) as caught:
        raise_for_status(_resp(429, headers={"retry-after": value} if value else {}), "acme")
    assert caught.value.retry_after == expected


def test_every_allowlisted_model_has_exactly_one_vendor() -> None:
    for model in DEFAULT_PRICES:
        if model != FAKE_MODEL:
            assert vendor_of(model) in VENDORS, model
            assert sum(model.startswith(v.model_prefix) for v in VENDORS.values()) == 1
    assert vendor_of(FAKE_MODEL) == "fake"
    assert vendor_of("mystery-1") == "unknown"


def test_registry_defaults_are_allowlisted_and_owned() -> None:
    for vendor in VENDORS.values():
        assert vendor.default_model in DEFAULT_PRICES
        assert vendor_of(vendor.default_model) == vendor.name


@pytest.mark.parametrize(("name", "cls"), [("openai", OpenAIProvider), ("anthropic", AnthropicProvider)])
def test_make_provider_builds_each_vendor_from_its_key_env(name: str, cls: type) -> None:
    vendor = VENDORS[name]
    settings = load_settings(None, env={vendor.key_env: "sk-test"})
    provider = make_provider(AgentSpec(name="a", provider=name), settings, InMemoryMetrics())
    assert isinstance(provider._inner, cls)  # type: ignore[attr-defined]
    assert settings.model_for(AgentSpec(name="a", provider=name)) == vendor.default_model


def test_make_provider_rejects_unknown_provider_and_missing_key() -> None:
    settings = load_settings(None, env={})
    with pytest.raises(ConfigError, match="unknown provider"):
        make_provider(AgentSpec(name="a", provider="acme"), settings, InMemoryMetrics())
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        make_provider(AgentSpec(name="a", provider="anthropic"), settings, InMemoryMetrics())


def test_model_env_override_is_generated_per_vendor() -> None:
    settings = load_settings(None, env={"DUEL_OPENAI_MODEL": "gpt-6-luna", "OPENAI_API_KEY": "k"})
    assert settings.model_for(AgentSpec(name="a", provider="openai")) == "gpt-6-luna"
