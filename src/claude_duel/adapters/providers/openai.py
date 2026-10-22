"""OpenAI Chat Completions adapter (plain HTTP, no SDK)."""

from __future__ import annotations

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
        body = {
            "model": self._model,
            "max_completion_tokens": request.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
        }
        try:
            resp = self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("openai timeout") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(f"openai transport: {type(exc).__name__}") from exc
        raise_for_status(resp)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage", {})
            return Completion(
                text=text,
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TransientProviderError("openai returned an unexpected body") from exc
