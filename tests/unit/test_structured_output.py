"""Vendor structured-output modes, truncation flags and the gpt-6 Responses path (fake transports)."""

from __future__ import annotations

import json

import httpx
from pydantic import SecretStr

from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.app.agents import ANSWER_SCHEMAS
from claude_duel.ports import CompletionRequest

KEY = SecretStr("sk-test-0000")
REQ = CompletionRequest(
    system="sys", prompt="go", purpose="attack", max_tokens=900, json_schema=ANSWER_SCHEMAS["attack"]
)


def _mock(seen: list[dict[str, object]], reply: dict[str, object]) -> httpx.MockTransport:
    def h(req: httpx.Request) -> httpx.Response:
        seen.append({"path": req.url.path, **json.loads(req.content)})
        return httpx.Response(200, json=reply)

    return httpx.MockTransport(h)


def test_anthropic_sends_schema_and_flags_max_tokens_stop() -> None:
    seen: list[dict[str, object]] = []
    reply = {"content": [{"type": "text", "text": "{"}], "stop_reason": "max_tokens", "usage": {"output_tokens": 900}}
    out = AnthropicProvider(KEY, "claude-haiku-5-5", transport=_mock(seen, reply)).complete(REQ)
    assert seen[0]["output_config"] == {"format": {"type": "json_schema", "schema": ANSWER_SCHEMAS["attack"]}}
    assert out.truncated


def test_anthropic_without_schema_sends_no_output_config() -> None:
    seen: list[dict[str, object]] = []
    reply = {"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn", "usage": {}}
    out = AnthropicProvider(KEY, "claude-haiku-5-5", transport=_mock(seen, reply)).complete(
        REQ.model_copy(update={"json_schema": None})
    )
    assert "output_config" not in seen[0]
    assert not out.truncated


def test_gpt6_uses_responses_api_without_temperature() -> None:
    seen: list[dict[str, object]] = []
    reply = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": '{"payload": "x", "notes": ""}'}]},
        ],
        "usage": {"input_tokens": 40, "output_tokens": 300},
    }
    out = OpenAIProvider(KEY, "gpt-6-luna", transport=_mock(seen, reply)).complete(REQ)
    body = seen[0]
    assert body["path"] == "/v1/responses"
    assert "temperature" not in body
    assert body["max_output_tokens"] == 900
    assert body["text"] == {
        "format": {"type": "json_schema", "name": "answer", "strict": True, "schema": REQ.json_schema}
    }
    assert (out.text, out.input_tokens, out.output_tokens, out.truncated) == (
        '{"payload": "x", "notes": ""}',
        40,
        300,
        False,
    )


def test_responses_incomplete_at_cap_is_truncated() -> None:
    reply = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [], "usage": {}}
    out = OpenAIProvider(KEY, "gpt-5.6-sol", transport=_mock([], reply)).complete(REQ)
    assert out.truncated
    assert not out.text


def test_responses_without_schema_falls_back_to_json_mode() -> None:
    seen: list[dict[str, object]] = []
    reply = {"status": "completed", "output": [], "usage": {}}
    OpenAIProvider(KEY, "gpt-6-astra", transport=_mock(seen, reply)).complete(
        REQ.model_copy(update={"json_schema": None})
    )
    assert seen[0]["text"] == {"format": {"type": "json_object"}}


def test_chat_models_keep_chat_completions_and_flag_length() -> None:
    seen: list[dict[str, object]] = []
    reply = {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 5}}
    out = OpenAIProvider(KEY, "gpt-4o-mini", transport=_mock(seen, reply)).complete(REQ)
    assert seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["temperature"] == 0.0
    assert out.truncated


def test_effort_is_sent_to_both_vendors_and_reasoning_tokens_are_counted() -> None:
    req = REQ.model_copy(update={"reasoning_effort": "low"})
    seen: list[dict[str, object]] = []
    claude_reply = {
        "content": [{"type": "text", "text": "{}"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 9, "output_tokens": 50, "output_tokens_details": {"thinking_tokens": 30}},
    }
    out = AnthropicProvider(KEY, "claude-haiku-5-5", transport=_mock(seen, claude_reply)).complete(req)
    assert seen[0]["output_config"] == {"format": {"type": "json_schema", "schema": REQ.json_schema}, "effort": "low"}
    assert (out.output_tokens, out.reasoning_tokens) == (50, 30)
    gpt_reply = {
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 9, "output_tokens": 70, "output_tokens_details": {"reasoning_tokens": 64}},
    }
    out = OpenAIProvider(KEY, "gpt-6-luna", transport=_mock(seen, gpt_reply)).complete(req)
    assert seen[1]["reasoning"] == {"effort": "low", "summary": "auto"}
    assert (out.output_tokens, out.reasoning_tokens) == (70, 64)
