"""gpt-5 family on a reasoning path (design-reasoning-models.md); fake transport only, no live call."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.app.budget import BudgetLimits
from claude_duel.app.estimate import estimate
from claude_duel.config import Settings
from claude_duel.domain.errors import PermanentProviderError
from claude_duel.domain.modes import MODES
from claude_duel.ports import CompletionRequest

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


def test_default_model_goes_to_responses_without_temperature() -> None:
    model = Settings().default_model("openai")
    assert model.startswith(("gpt-5", "gpt-6"))
    sent: list[httpx.Request] = []
    _provider(model, RESP, sent).complete(REQ)
    body = json.loads(sent[0].content)
    assert sent[0].url.path.endswith("/responses")
    assert "temperature" not in body
    assert body["reasoning"] == {"effort": "minimal", "summary": "auto"}


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
