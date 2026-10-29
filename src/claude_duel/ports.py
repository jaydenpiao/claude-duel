"""Ports (hexagonal boundaries). Adapters implement these Protocols."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from claude_duel.domain.models import Event, MatchState, SandboxLimits

Purpose = Literal["build", "attack"]


# OpenAI routing (#191): a model is sent to /responses by prefix, or to /chat only if listed; config rejects the rest.
RESPONSES_MODELS = ("gpt-6", "gpt-5")  # reasoning families (gpt-5, -nano, -mini, gpt-5.x) served via /responses
CHAT_MODELS = frozenset({"gpt-4.1-nano", "gpt-4o-mini", "gpt-4.1-mini"})  # the only OpenAI models sent to /chat


class CompletionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    system: str
    prompt: str
    purpose: Purpose
    max_tokens: int = 1500
    temperature: float = 0.0
    reasoning_effort: str | None = None  # vendor effort level (low/medium/high); None = vendor default
    json_schema: dict[str, Any] | None = Field(
        default=None, description="strict answer schema; adapters send it as the vendor's structured-output mode"
    )


class Completion(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0  # thinking/reasoning tokens, already inside output_tokens and billed as output
    truncated: bool = False  # the vendor stopped at max_tokens, so the answer is cut off
    reasoning_effort: str | None = None  # the effort the adapter actually sent (None = vendor default)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider(Protocol):
    name: str

    def complete(self, request: CompletionRequest) -> Completion: ...


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    stdout: str
    exit_code: int
    timed_out: bool
    killed_reason: str | None = None


class Sandbox(Protocol):
    def run(self, source: str, stdin: str, flag: str, limits: SandboxLimits) -> RunResult:
        """Run challenge source with the flag exposed only as its global ``FLAG``."""
        ...


class MatchStore(Protocol):
    def save(self, state: MatchState) -> None: ...

    def load(self, match_id: str) -> MatchState: ...

    def lock(self, match_id: str) -> MatchLock: ...


class MatchLock(Protocol):
    def __enter__(self) -> MatchLock: ...

    def __exit__(self, *exc: object) -> None: ...


class Metrics(Protocol):
    def incr(self, name: str, value: int = 1, **tags: str) -> None: ...

    def timing(self, name: str, seconds: float, **tags: str) -> None: ...


class SpendLedger(Protocol):
    def total_usd(self) -> float: ...

    def spent_usd(self, prefix: str) -> float: ...

    def reserve(self, match_id: str, model: str, usd: float) -> str: ...

    def record(self, match_id: str, model: str, tokens: int, usd: float, settles: str | None = None) -> None: ...


class EventLog(Protocol):
    """Append-only per-match event log; the source of truth for replay (ADR 0004)."""

    def append(self, match_id: str, event: Event) -> None: ...

    def read(self, match_id: str) -> list[Event]: ...


EventObserver = Callable[[Event], None]
"""Called with each event after it is durably appended (e.g. the CLI commentary)."""
