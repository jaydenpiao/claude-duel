"""Anthropic adapter (#10): same port as OpenAI, caching off, usage into the ledger, premium gating."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.retry import RetryingProvider
from claude_duel.app.budget import DEFAULT_PRICES, PREMIUM, check_model_allowed, vendor_of
from claude_duel.app.container import make_provider
from claude_duel.config import Settings, load_settings
from claude_duel.domain.errors import (
    ConfigError,
    PermanentProviderError,
    ProviderTimeoutError,
    QuotaExceededError,
    RateLimitError,
    TransientProviderError,
)
from claude_duel.domain.models import AgentSpec
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import CompletionRequest

REQ = CompletionRequest(system="sys", prompt="TASK: BUILD", purpose="build", max_tokens=700)
KEY = SecretStr("sk-ant-test-0000")


def _claude(handler: httpx.MockTransport) -> AnthropicProvider:
    return AnthropicProvider(KEY, "claude-haiku-5-5", transport=handler)


def test_request_shape_and_usage() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/messages"
        assert req.headers["x-api-key"] == "sk-ant-test-0000"
        assert req.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(req.content)
        assert body == {
            "model": "claude-haiku-5-5",
            "max_tokens": 700,
            "system": "sys",
            "messages": [{"role": "user", "content": "TASK: BUILD"}],
        }  # no temperature, no cache_control: vendor-default sampling, caching off
        content = [{"type": "thinking", "thinking": "hmm", "signature": "x"}, {"type": "text", "text": '{"a": 1}'}]
        return httpx.Response(200, json={"content": content, "usage": {"input_tokens": 11, "output_tokens": 5}})

    c = _claude(httpx.MockTransport(h)).complete(REQ)
    assert (c.text, c.input_tokens, c.output_tokens) == ('{"a": 1}', 11, 5)
    assert c.reasoning_summary == "hmm"  # the thinking block (a summary on current Claude models) is kept


def test_cache_tokens_count_as_input() -> None:
    usage = {"input_tokens": 2, "output_tokens": 1, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}
    body = {"content": [{"type": "text", "text": "x"}], "usage": usage}
    c = _claude(httpx.MockTransport(lambda r: httpx.Response(200, json=body))).complete(REQ)
    assert c.input_tokens == 9


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (429, {}, RateLimitError),
        (529, {}, TransientProviderError),
        (500, {}, TransientProviderError),
        (401, {}, PermanentProviderError),
        (400, {"error": {"message": "Your credit balance is too low"}}, QuotaExceededError),
    ],
)
def test_status_mapping(status: int, body: dict[str, object], exc: type[Exception]) -> None:
    p = _claude(httpx.MockTransport(lambda r: httpx.Response(status, json=body)))
    with pytest.raises(exc):
        p.complete(REQ)


def test_timeout_transport_and_bad_body() -> None:
    def timeout(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def reset(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset")

    with pytest.raises(ProviderTimeoutError):
        _claude(httpx.MockTransport(timeout)).complete(REQ)
    with pytest.raises(TransientProviderError):
        _claude(httpx.MockTransport(reset)).complete(REQ)
    with pytest.raises(TransientProviderError):
        _claude(httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1}))).complete(REQ)


def test_premium_needs_opt_in_and_vendor_matches(tmp_path: Path) -> None:
    for model in PREMIUM:
        assert model in DEFAULT_PRICES
        with pytest.raises(ConfigError, match="premium"):
            check_model_allowed(model)
        check_model_allowed(model, allow_premium=True)
    assert (vendor_of("claude-fable-5-1"), vendor_of("gpt-6-astra"), vendor_of("fake-1")) == (
        "anthropic",
        "openai",
        "fake",
    )
    cfg = tmp_path / "c.toml"
    agents = '[[agents]]\nname = "a"\nprovider = "anthropic"\nmodel = "claude-fable-5-1"\n[[agents]]\nname = "b"\n'
    cfg.write_text(agents)
    with pytest.raises(ConfigError, match="premium"):
        load_settings(cfg, {})
    cfg.write_text("allow_premium = true\n" + agents)
    assert load_settings(cfg, {}).model_for(load_settings(cfg, {}).agents[0]) == "claude-fable-5-1"
    cfg.write_text('[[agents]]\nname = "a"\nprovider = "anthropic"\nmodel = "gpt-6-luna"\n[[agents]]\nname = "b"\n')
    with pytest.raises(ConfigError, match="not a anthropic model"):
        load_settings(cfg, {})


def test_key_from_env_only_and_wiring() -> None:
    s = load_settings(None, {"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert s.anthropic_api_key is not None
    assert "sk-ant-x" not in repr(s)
    agent = AgentSpec(name="c", provider="anthropic")
    assert isinstance(make_provider(agent, s, InMemoryMetrics()), RetryingProvider)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_provider(agent, Settings(), InMemoryMetrics())
    with pytest.raises(ConfigError, match="unknown provider"):
        make_provider(AgentSpec(name="d", provider="nope"), s, InMemoryMetrics())
