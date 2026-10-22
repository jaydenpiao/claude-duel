"""OpenAI adapter (plain HTTP, no SDK).

Models in ``RESPONSES_MODELS`` (the gpt-6 and gpt-5.6 reasoning families) go through the Responses API: no
temperature (reasoning models reject it), ``max_output_tokens`` (reasoning included) and the request's strict
``json_schema`` as structured output. Older models keep Chat Completions with JSON mode at temperature 0.
Either way a reply stopped by the token cap is marked ``truncated``.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from claude_duel.domain.errors import (
    PermanentProviderError,
    ProviderTimeoutError,
    QuotaExceededError,
    RateLimitError,
    TransientProviderError,
)
from claude_duel.ports import Completion, CompletionRequest

RESPONSES_MODELS = ("gpt-6", "gpt-5.6")  # model-name prefixes served via /responses


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def raise_for_status(resp: httpx.Response) -> None:
    """Map HTTP status to the provider error taxonomy (shared by HTTP adapters)."""
    if resp.status_code == 429 and "insufficient_quota" in resp.text:
        raise QuotaExceededError("openai quota exhausted or spend limit reached; top up or raise the limit")
    if resp.status_code == 429:
        raise RateLimitError("rate limited", _retry_after(resp))
    if resp.status_code >= 500 or resp.status_code == 408:
        raise TransientProviderError(f"server error {resp.status_code}")
    if resp.status_code >= 400:
        raise PermanentProviderError(f"client error {resp.status_code}")


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
        try:
            resp = self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("openai timeout") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(f"openai transport: {type(exc).__name__}") from exc
        raise_for_status(resp)
        try:
            data = resp.json()
            return _parse_responses(data) if responses else _parse_chat(data)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TransientProviderError("openai returned an unexpected body") from exc

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
        if request.reasoning_effort is not None:
            body["reasoning"] = {"effort": request.reasoning_effort}
        return body


def _parse_chat(data: Any) -> Completion:
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    return Completion(
        text=choice["message"]["content"] or "",
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
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
    )
