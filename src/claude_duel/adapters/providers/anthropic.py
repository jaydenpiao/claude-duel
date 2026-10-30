"""Anthropic Messages API adapter (plain HTTP, no SDK), behind the same ``LLMProvider`` port as OpenAI.

Prompt caching stays off: no ``cache_control`` is ever sent, so every input token is billed at the base rate
and both vendors see the same uncached prompts. A request's ``json_schema`` goes out as structured output
(``output_config.format``); ``stop_reason == "max_tokens"`` marks the reply truncated.
Thinking blocks are dropped; only text blocks are the answer.
Sampling is left at the vendor default (current Claude models reject any temperature other than 1.0).
"""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from claude_duel.adapters.providers.openai import raise_for_status
from claude_duel.domain.errors import ProviderTimeoutError, QuotaExceededError, TransientProviderError
from claude_duel.ports import Completion, CompletionRequest

API_VERSION = "2023-06-01"
OVERLOADED = 529  # Anthropic's "overloaded" status: retryable


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"x-api-key": api_key.get_secret_value(), "anthropic-version": API_VERSION},
        )

    def complete(self, request: CompletionRequest) -> Completion:
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        config: dict[str, object] = {}
        if request.json_schema is not None:
            config["format"] = {"type": "json_schema", "schema": request.json_schema}
        if request.reasoning_effort is not None:
            config["effort"] = request.reasoning_effort
        if config:
            body["output_config"] = config
        try:
            resp = self._client.post("/messages", json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("anthropic timeout") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(f"anthropic transport: {type(exc).__name__}") from exc
        if resp.status_code == OVERLOADED:
            raise TransientProviderError("anthropic overloaded")
        if resp.status_code in (400, 402, 403) and "credit balance" in resp.text:
            raise QuotaExceededError("anthropic credit balance too low; top up or raise the limit")
        raise_for_status(resp)
        return _parse(resp)


def _parse(resp: httpx.Response) -> Completion:
    try:
        data = resp.json()
        text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        usage = data.get("usage", {})
        # Cache fields stay 0 with caching off; if present they are still input, billed at base rate (upper bound).
        cached = int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
        return Completion(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)) + cached,
            output_tokens=int(usage.get("output_tokens", 0)),
            reasoning_tokens=int((usage.get("output_tokens_details") or {}).get("thinking_tokens", 0)),
            truncated=data.get("stop_reason") == "max_tokens",
            reasoning_summary=" ".join(b.get("thinking", "") for b in data["content"] if b.get("type") == "thinking"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TransientProviderError("anthropic returned an unexpected body") from exc
