"""OpenAI adapter (plain HTTP, no SDK).

Models in ``RESPONSES_MODELS`` (the gpt-6 and gpt-5 reasoning families) go through the Responses API: no
temperature (reasoning models reject it), ``max_output_tokens`` (reasoning included) and the request's strict
``json_schema`` as structured output. Older models keep Chat Completions with JSON mode at temperature 0.
Either way a reply stopped by the token cap is marked ``truncated``.
A content-filter finish, ``message.refusal``, a refusal part, or a Responses
content-filter reason is marked ``refused``.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from claude_duel.adapters.providers.http import raise_for_status
from claude_duel.domain.errors import ProviderTimeoutError, TransientProviderError
from claude_duel.ports import RESPONSES_MODELS, Completion, CompletionRequest

HTTP_BAD_REQUEST = 400


_PROCESS_ID = uuid.uuid4().hex  # fallback when the store dir is not writable (read-only image, odd HOME)


def install_id_path(store_dir: Path = Path(".duel")) -> Path:
    """``install-id`` in the store dir the composition root passes (``settings.store_dir``); never under HOME."""
    return store_dir / "install-id"


def _read_id(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def _install_id(path: Path) -> str:
    """Read the id, else create it atomically at 0o600 (written to a private O_EXCL temp, then linked in, so no
    reader ever sees an empty file). An empty file counts as missing."""
    if raw := _read_id(path):
        return raw
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".install-id.{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(uuid.uuid4().hex)
        try:
            os.link(tmp, path)
        except FileExistsError:
            if not _read_id(path):
                os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return _read_id(path)


def default_safety_identifier(path: Path | None = None) -> str:
    """Stable sha256 of a random per-install id (never a hostname, username, or other personal data); a random
    per-process id when the store dir cannot be written."""
    try:
        raw = _install_id(path or install_id_path())
    except OSError:
        raw = ""
    return hashlib.sha256((raw or _PROCESS_ID).encode()).hexdigest()


# Small gpt-5 models default to minimal effort, so reasoning cannot eat a short output cap (design-reasoning-models.md).
# Exact prefixes on purpose: gpt-5.1+ names (e.g. gpt-5.1-mini) do not accept ``minimal`` (they use ``none``), so do not
# widen these to match them. nano/mini still accept "minimal"; gpt-6-luna does not (documented migration is "low").
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
        safety_identifier: str | None = None,
    ) -> None:
        self._model = model
        self._safety_identifier = safety_identifier if safety_identifier is not None else default_safety_identifier()
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
            if not responses:
                return _parse_chat(data)
            effort = default_effort(self._model, request.reasoning_effort)
            return _parse_responses(data).model_copy(update={"reasoning_effort": effort})
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
            "safety_identifier": self._safety_identifier,
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
            "safety_identifier": self._safety_identifier,
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


def _chat_text(message: object) -> str:
    """Plain text of a chat message; list content keeps only ``text`` parts (refusals are the flag, not the answer)."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _has_refusal_part(content: object) -> bool:
    """True when ``content`` is a list that includes a ``refusal`` part."""
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "refusal" for part in content
    )


def _chat_refused(message: object, finish_reason: object) -> bool:
    """True on a content-filter stop, a non-empty ``refusal`` field, or a refusal content part."""
    if finish_reason == "content_filter":
        return True
    if not isinstance(message, dict):
        return False
    if message.get("refusal"):
        return True
    return _has_refusal_part(message.get("content"))


def _responses_refused(items: object, reason: object) -> bool:
    """True on a content-filter incomplete reason or a refusal output part."""
    if reason == "content_filter":
        return True
    if not isinstance(items, list):
        return False
    return any(
        _has_refusal_part(item.get("content"))
        for item in items
        if isinstance(item, dict) and item.get("type") == "message"
    )


def _parse_chat(data: Any) -> Completion:
    choice = data["choices"][0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    return Completion(
        text=_chat_text(message),
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)),
        truncated=choice.get("finish_reason") == "length",
        refused=_chat_refused(message, choice.get("finish_reason")),
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
        refused=_responses_refused(items, reason),
        reasoning_summary=" ".join(
            part.get("text", "")
            for item in items
            if item.get("type") == "reasoning"
            for part in item.get("summary") or []
            if part.get("type") == "summary_text"
        ),
    )
