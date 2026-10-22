"""Ports (hexagonal boundaries). Adapters implement these Protocols."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from claude_duel.domain.models import Event, MatchState, SandboxLimits
from claude_duel.domain.results import MatchProjection, RatingRow, RunCounts, RunStatus, Stored

Purpose = Literal["build", "attack"]


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

    def snapshot(self, match_id: str) -> MatchState:
        """The saved state without its secret; the secret file is never opened (``duel sync``)."""
        ...


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

    def ids(self) -> list[str]: ...

    def chain(self, match_id: str) -> list[tuple[str, Event]]:
        """Verified ``(hash, event)`` pairs in order; raises a ``DuelError`` if the chain is broken."""
        ...


EventObserver = Callable[[Event], None]
"""Called with each event after it is durably appended (e.g. the CLI commentary)."""


Row = dict[str, object]


class ResultsStore(Protocol):
    """Derived results index fed by ``duel sync`` (ADR 0012); rebuildable from the event logs.

    Run ids are client-generated (uuid hex) so a run is identifiable even if the DB call that records it fails.
    """

    def stored(self, match_id: str) -> Stored: ...

    def ingest(self, projection: MatchProjection) -> int:
        """Write one match atomically and idempotently; return how many new events were inserted."""
        ...

    def start_run(self, run_id: str, git_sha: str | None) -> None: ...

    def reject(self, run_id: str, match_id: str, reason: str) -> None:
        """Record why a match was not loaded and hide any earlier copy of it from the public views."""
        ...

    def last_ratings_digest(self) -> str | None:
        """Digest of the inputs behind the newest ``ok`` run's ratings snapshot, if any."""
        ...

    def finish_run(
        self, run_id: str, status: RunStatus, counts: RunCounts, ratings: list[RatingRow], digest: str | None
    ) -> None: ...

    def rebuilding(self) -> AbstractContextManager[None]:
        """Empty the derived tables and reload inside one transaction; history and first-seen times are kept."""
        ...


class PublicSource(Protocol):
    """The read-only public views, read by a role that cannot write (ADR 0012)."""

    def export(self) -> dict[str, list[Row]]: ...
