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

RESPONSES_MODELS = ("gpt-6", "gpt-5")  # reasoning families (gpt-5, -nano, -mini, gpt-5.x) served via /responses
# Small gpt-5 models default to minimal effort, so reasoning cannot eat a short output cap (design-reasoning-models.md).
MINIMAL_EFFORT_MODELS = ("gpt-5-nano", "gpt-5-mini")


def default_effort(model: str, effort: str | None) -> str | None:
    """The effort sent: the request's own, else ``minimal`` for the small gpt-5 models, else vendor default."""
    if effort is not None:
        return effort
    return "minimal" if model.startswith(MINIMAL_EFFORT_MODELS) else None


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
        raise_for_status(resp, "openai")
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
        effort = default_effort(self._model, request.reasoning_effort)
        if effort is not None:
            body["reasoning"] = {"effort": effort}
        return body


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
    )
