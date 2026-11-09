"""OpenAI adapter (plain HTTP, no SDK).

Models in ``RESPONSES_MODELS`` (the gpt-6 and gpt-5 reasoning families) go through the Responses API: no
temperature (reasoning models reject it), ``max_output_tokens`` (reasoning included) and the request's strict
``json_schema`` as structured output. Older models keep Chat Completions with JSON mode at temperature 0.
Either way a reply stopped by the token cap is marked ``truncated``.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from claude_duel.adapters.providers.http import raise_for_status
from claude_duel.domain.errors import ProviderTimeoutError, TransientProviderError
from claude_duel.ports import Completion, CompletionRequest

HTTP_BAD_REQUEST = 400
RESPONSES_MODELS = ("gpt-6", "gpt-5")  # reasoning families (gpt-5, -nano, -mini, gpt-5.x) served via /responses
# nano/mini still accept "minimal". gpt-6-luna does not; the documented migration from minimal is "low".
MINIMAL_EFFORT_MODELS = ("gpt-5-nano", "gpt-5-mini")
LOW_EFFORT_MODELS = ("gpt-6-luna",)


def default_effort(model: str, effort: str | None) -> str | None:
    """The effort sent: the request's own, else the small-model default, else the vendor default."""
    if effort is not None:
        return effort
    if model.startswith(MINIMAL_EFFORT_MODELS):
        return "minimal"
    if model.startswith(LOW_EFFORT_MODELS):
        return "low"
    return None


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._summaries = True  # ask for reasoning summaries until the org turns out not to be verified for them
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
        )

    def complete(self, request: CompletionRequest) -> Completion:
        responses = self._model.startswith(RESPONSES_MODELS)
        path, body = (
            ("/responses", self._responses_body(request))
            if responses
            else ("/chat/completions", self._chat_body(request))
        )
        resp = self._post(path, body)
        if responses and _summary_refused(resp, body):
            self._summaries = False  # unverified org: drop the summary request for good, retry once (400s cost nothing)
            resp = self._post(path, self._responses_body(request))
        raise_for_status(resp, "openai")
        try:
            data = resp.json()
            return _parse_responses(data) if responses else _parse_chat(data)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TransientProviderError("openai returned an unexpected body") from exc

    def _post(self, path: str, body: dict[str, object]) -> httpx.Response:
        try:
            return self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("openai timeout") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(f"openai transport: {type(exc).__name__}") from exc

    def _chat_body(self, request: CompletionRequest) -> dict[str, object]:
        return {
            "model": self._model,
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
        }

    def _responses_body(self, request: CompletionRequest) -> dict[str, object]:
        fmt: dict[str, object] = (
            {"type": "json_schema", "name": "answer", "strict": True, "schema": request.json_schema}
            if request.json_schema is not None
            else {"type": "json_object"}
        )
        body: dict[str, object] = {
            "model": self._model,
            "instructions": request.system,
            "input": request.prompt,
            "max_output_tokens": request.max_tokens,
            "text": {"format": fmt},
            "store": False,
        }
        effort = default_effort(self._model, request.reasoning_effort)
        reasoning = ({"effort": effort} if effort is not None else {}) | (
            {"summary": "auto"} if self._summaries else {}
        )
        if reasoning:
            body["reasoning"] = reasoning
        return body


def _summary_refused(resp: httpx.Response, body: dict[str, object]) -> bool:
    """A 400 that rejects ``reasoning.summary`` (OpenAI requires a verified organization for summaries)."""
    reasoning = body.get("reasoning")
    if resp.status_code != HTTP_BAD_REQUEST or not isinstance(reasoning, dict) or "summary" not in reasoning:
        return False
    text = resp.text.lower()
    return "summary" in text or "verif" in text


def _parse_chat(data: Any) -> Completion:
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    return Completion(
        text=choice["message"]["content"] or "",
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)),
        truncated=choice.get("finish_reason") == "length",
    )


def _parse_responses(data: Any) -> Completion:
    items = data.get("output") or []
    text = "".join(
        part.get("text", "")
        for item in items
        if item.get("type") == "message"
        for part in item.get("content") or []
        if part.get("type") == "output_text"
    )
    usage = data.get("usage") or {}
    reason = (data.get("incomplete_details") or {}).get("reason")
    return Completion(
        text=text,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        reasoning_tokens=int((usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)),
        truncated=data.get("status") == "incomplete" and reason == "max_output_tokens",
        reasoning_summary=" ".join(
            part.get("text", "")
            for item in items
            if item.get("type") == "reasoning"
            for part in item.get("summary") or []
            if part.get("type") == "summary_text"
        ),
    )
