"""gpt-5 family on a reasoning path (design-reasoning-models.md); fake transport only, no live call."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.app import budget
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, check_model_allowed, vendor_of
from claude_duel.app.estimate import estimate
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError, PermanentProviderError
from claude_duel.domain.modes import MODES
from claude_duel.ports import CHAT_MODELS, RESPONSES_MODELS, CompletionRequest

REQ = CompletionRequest(system="s", prompt="p", purpose="build", max_tokens=100)


def _provider(model: str, reply: dict[str, object], sent: list[httpx.Request]) -> OpenAIProvider:
    def h(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json=reply)

    return OpenAIProvider(SecretStr("sk-test-0000000000"), model, transport=httpx.MockTransport(h))


RESP = {
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
    "usage": {"input_tokens": 5, "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 30}},
}


# Published on the gpt-6-luna model page. "minimal" is absent, so a default payload that uses it is rejected.
LUNA_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})


def _body(req: httpx.Request) -> dict[str, object]:
    payload: object = json.loads(req.content)
    return payload if isinstance(payload, dict) else {}


def _effort(body: dict[str, object]) -> object:
    reasoning = body.get("reasoning")
    return reasoning.get("effort") if isinstance(reasoning, dict) else None


def _rejecting(model: str, sent: list[httpx.Request], allow: Callable[[dict[str, object]], bool]) -> OpenAIProvider:
    def h(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        body = _body(req)
        if not allow(body):
            return httpx.Response(400, json={"error": {"message": f"rejected reasoning.effort: {_effort(body)}"}})
        return httpx.Response(200, json=RESP)

    return OpenAIProvider(SecretStr("sk-test-0000000000"), model, transport=httpx.MockTransport(h))


def test_default_model_goes_to_responses_without_temperature() -> None:
    model = Settings().default_model("openai")
    assert model == "gpt-6-luna"
    sent: list[httpx.Request] = []
    _provider(model, RESP, sent).complete(REQ)
    body = json.loads(sent[0].content)
    assert sent[0].url.path.endswith("/responses")
    assert "temperature" not in body
    assert "service_tier" not in body
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}


def _luna_allows(body: dict[str, object]) -> bool:
    return _effort(body) in LUNA_EFFORTS and "service_tier" not in body and "temperature" not in body


def test_luna_default_is_rejected_if_it_sends_an_unsupported_effort() -> None:
    """A 400 for ``minimal`` (or any effort outside the luna page) must not be the default payload."""
    sent: list[httpx.Request] = []
    assert _rejecting("gpt-6-luna", sent, _luna_allows).complete(REQ).text == "{}"
    assert _effort(_body(sent[0])) == "low"
    assert len(sent) == 1


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_explicit_luna_effort_passes_the_supported_set(effort: str) -> None:
    sent: list[httpx.Request] = []
    req = REQ.model_copy(update={"reasoning_effort": effort})
    assert _rejecting("gpt-6-luna", sent, _luna_allows).complete(req).text == "{}"
    assert _effort(_body(sent[0])) == effort


@pytest.mark.parametrize("model", ["gpt-5-nano", "gpt-5-mini"])
def test_pinned_nano_and_mini_still_default_to_minimal(model: str) -> None:
    sent: list[httpx.Request] = []
    assert _rejecting(model, sent, lambda body: _effort(body) == "minimal").complete(REQ).text == "{}"
    assert _effort(_body(sent[0])) == "minimal"


@pytest.mark.parametrize("model", ["gpt-5-nano", "gpt-5-mini-2026", "gpt-5", "gpt-5.6-sol"])
def test_gpt5_family_routes_to_responses(model: str) -> None:
    sent: list[httpx.Request] = []
    _provider(model, RESP, sent).complete(REQ)
    assert sent[0].url.path.endswith("/responses")


def test_explicit_effort_wins_and_big_models_keep_vendor_default() -> None:
    sent: list[httpx.Request] = []
    _provider("gpt-5-nano", RESP, sent).complete(REQ.model_copy(update={"reasoning_effort": "high"}))
    _provider("gpt-5.6-sol", RESP, sent).complete(REQ)
    assert json.loads(sent[0].content)["reasoning"] == {"effort": "high", "summary": "auto"}
    assert json.loads(sent[1].content)["reasoning"] == {"summary": "auto"}  # vendor-default effort


def test_completion_echoes_the_effort_actually_sent() -> None:
    assert _provider("gpt-5-nano", RESP, []).complete(REQ).reasoning_effort == "minimal"
    high = REQ.model_copy(update={"reasoning_effort": "high"})
    assert _provider("gpt-5-nano", RESP, []).complete(high).reasoning_effort == "high"
    assert _provider("gpt-5.6-sol", RESP, []).complete(REQ).reasoning_effort is None


def test_responses_reasoning_tokens_parsed() -> None:
    c = _provider("gpt-5-nano", RESP, []).complete(REQ)
    assert 0 < c.reasoning_tokens <= c.output_tokens


def test_chat_reasoning_tokens_parsed() -> None:
    chat = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 12}},
    }
    sent: list[httpx.Request] = []
    c = _provider("gpt-4o-mini", chat, sent).complete(REQ)
    assert sent[0].url.path.endswith("/chat/completions")
    assert 0 < c.reasoning_tokens <= c.output_tokens


def test_reasoning_that_fills_the_cap_is_truncated() -> None:
    eaten = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "summary": []}],
        "usage": {"input_tokens": 5, "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 100}},
    }
    c = _provider("gpt-5-nano", eaten, []).complete(REQ)
    assert c.truncated
    assert c.text == ""


@pytest.mark.parametrize("mode", sorted(MODES))
def test_estimate_for_gpt5_nano_within_match_cap(mode: str) -> None:
    limits = BudgetLimits()
    assert estimate(MODES[mode], "gpt-5-nano", limits).usd <= limits.match_max_usd


def test_every_allowlisted_openai_model_has_a_route() -> None:
    for m in (m for m in DEFAULT_PRICES if vendor_of(m) == "openai"):
        assert m.startswith(RESPONSES_MODELS) or m in CHAT_MODELS, m
        check_model_allowed(m, allow_premium=True)


def test_openai_model_without_a_route_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(budget.DEFAULT_PRICES, "gpt-4-turbo", (1.0, 1.0))
    with pytest.raises(ConfigError, match="no OpenAI route"):
        budget.check_model_allowed("gpt-4-turbo")


UNVERIFIED = {
    "error": {
        "message": "Your organization must be verified to generate reasoning summaries.",
        "type": "invalid_request_error",
        "param": "reasoning.summary",
    }
}


def test_unverified_org_falls_back_without_summaries_once() -> None:
    sent: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        wants = "summary" in json.loads(req.content).get("reasoning", {})
        return httpx.Response(400, json=UNVERIFIED) if wants else httpx.Response(200, json=RESP)

    p = OpenAIProvider(SecretStr("sk-test-0000000000"), "gpt-5-nano", transport=httpx.MockTransport(h))
    assert p.complete(REQ).text == "{}"
    assert p.complete(REQ).text == "{}"
    bodies = [json.loads(r.content)["reasoning"] for r in sent]
    assert bodies == [{"effort": "minimal", "summary": "auto"}, {"effort": "minimal"}, {"effort": "minimal"}]


def test_other_400s_are_not_retried() -> None:
    sent: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(400, json={"error": {"message": "bad schema"}})

    p = OpenAIProvider(SecretStr("sk-test-0000000000"), "gpt-5-nano", transport=httpx.MockTransport(h))
    with pytest.raises(PermanentProviderError):
        p.complete(REQ)
    assert len(sent) == 1
