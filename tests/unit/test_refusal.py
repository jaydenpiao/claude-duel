"""Vendor refusal parsing, log omission, and labels (ADR 0016)."""

from __future__ import annotations

import json

import httpx
from pydantic import SecretStr

from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.app.engine import MALFORMED, REFUSED
from claude_duel.app.replay import ReplayReport
from claude_duel.domain import events as ev
from claude_duel.domain.models import AttackAttempt, Event
from claude_duel.domain.public import public_data
from claude_duel.ports import CompletionRequest
from claude_duel.presentation.card import card_data, card_svg
from tests.helpers import match_events

REQ = CompletionRequest(system="s", prompt="go", purpose="build", max_tokens=100)
ANT_KEY = SecretStr("sk-ant-test-0000")
OAI_KEY = SecretStr("sk-test-0000000000")


def _ant(body: dict[str, object]) -> AnthropicProvider:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    return AnthropicProvider(ANT_KEY, "claude-haiku-5-5", transport=transport)


def _oai(model: str, body: dict[str, object]) -> OpenAIProvider:
    return OpenAIProvider(OAI_KEY, model, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))


def test_anthropic_stop_reason_refusal() -> None:
    body = {"content": [{"type": "text", "text": "I can't help with that."}], "stop_reason": "refusal", "usage": {}}
    out = _ant(body).complete(REQ)
    assert out.refused
    assert not out.truncated
    assert out.text == "I can't help with that."


def test_anthropic_end_turn_is_not_refused() -> None:
    body = {"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn", "usage": {}}
    out = _ant(body).complete(REQ)
    assert not out.refused


def test_openai_chat_content_filter_and_refusal_field() -> None:
    filtered = {
        "choices": [{"message": {"content": None}, "finish_reason": "content_filter"}],
        "usage": {},
    }
    out = _oai("gpt-4o-mini", filtered).complete(REQ)
    assert out.refused
    assert out.text == ""

    field = {
        "choices": [{"message": {"content": None, "refusal": "Sorry, I can't."}, "finish_reason": "stop"}],
        "usage": {},
    }
    out = _oai("gpt-4o-mini", field).complete(REQ)
    assert out.refused

    part = {
        "choices": [
            {
                "message": {"content": [{"type": "refusal", "refusal": "no"}]},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    out = _oai("gpt-4o-mini", part).complete(REQ)
    assert out.refused
    assert out.text == ""


def test_openai_responses_refusal_part_and_content_filter() -> None:
    part = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
        "usage": {},
    }
    out = _oai("gpt-5-nano", part).complete(REQ)
    assert out.refused
    assert not out.truncated
    assert out.text == ""

    filtered = {
        "status": "incomplete",
        "incomplete_details": {"reason": "content_filter"},
        "output": [],
        "usage": {},
    }
    out = _oai("gpt-5-nano", filtered).complete(REQ)
    assert out.refused
    assert not out.truncated


def test_openai_normal_and_truncated_replies_are_not_refused() -> None:
    stop = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}}
    assert not _oai("gpt-4o-mini", stop).complete(REQ).refused
    length = {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}], "usage": {}}
    out = _oai("gpt-4o-mini", length).complete(REQ)
    assert out.truncated
    assert not out.refused


def _charged(seq: int, refused: bool | None = None) -> Event:
    data: dict[str, object] = {
        "agent": "alice",
        "model": "fake-1",
        "tokens": 2,
        "usd": 0.0,
        "elapsed_ms": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "truncated": False,
        "purpose": "build",
        "summary": "",
    }
    if refused:
        data["refused"] = True
    return Event(v=ev.SCHEMA_VERSION, seq=seq, type=ev.CALL_CHARGED, trace_id="t", data=data)


def test_payload_treats_missing_refused_as_false() -> None:
    p = ev.payload(_charged(1))
    assert isinstance(p, ev.CallCharged)
    assert p.refused is False
    assert "refused" not in _charged(1).data


def test_public_data_keeps_refused_only_when_present() -> None:
    assert "refused" not in public_data(_charged(1))
    assert public_data(_charged(1, refused=True))["refused"] is True


def test_card_labels_refusals_only_when_present() -> None:
    events = match_events()
    state = ev.fold("m", events)
    clean = ReplayReport(True, {}, {}, [], [])
    assert "model refusal" not in card_svg(card_data(state, clean, events))
    refused_events = [*events, _charged(1, refused=True)]
    svg = card_svg(card_data(state, ReplayReport(True, {}, {}, [], [], refusals=1), refused_events))
    assert "1 model refusal" in svg
    assert "refused" not in json.dumps(_charged(2).data)


def test_attempt_omits_refused_when_false() -> None:
    clean = AttackAttempt(payload="x", output="y", tokens=1)
    assert clean.refused is False
    assert "refused" not in clean.logged()
    refused = AttackAttempt(payload=MALFORMED, output=REFUSED, tokens=1, refused=True)
    assert refused.logged()["refused"] is True
    assert refused.refused
