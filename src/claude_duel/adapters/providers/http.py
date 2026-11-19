"""HTTP helpers shared by every plain-HTTP provider adapter."""

from __future__ import annotations

from typing import Any

import httpx

from claude_duel.domain.errors import (
    CyberPolicyError,
    PermanentProviderError,
    QuotaExceededError,
    RateLimitError,
    TransientProviderError,
)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _error_code(resp: httpx.Response) -> str | None:
    try:
        payload: Any = resp.json()
    except ValueError:
        return None
    err = payload.get("error") if isinstance(payload, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, str) else None


def raise_for_status(resp: httpx.Response, vendor: str) -> None:
    """Map HTTP status to the provider error taxonomy."""
    if resp.status_code >= 400 and (_error_code(resp) == "cyber_policy" or "cyber_policy" in resp.text):
        raise CyberPolicyError(
            f"{vendor} cyber_policy: access limited for this safety identifier or organization; not retried"
        )
    if resp.status_code == 429 and "insufficient_quota" in resp.text:
        raise QuotaExceededError(f"{vendor} quota exhausted or spend limit reached; top up or raise the limit")
    if resp.status_code == 429:
        raise RateLimitError("rate limited", _retry_after(resp))
    if resp.status_code >= 500 or resp.status_code == 408:
        raise TransientProviderError(f"server error {resp.status_code}")
    if resp.status_code >= 400:
        raise PermanentProviderError(f"client error {resp.status_code}")
