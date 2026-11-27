"""Ports (hexagonal boundaries). Adapters implement these Protocols."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from claude_duel.domain.models import Event, MatchState, Purpose, SandboxLimits
from claude_duel.domain.results import MatchProjection, RatingRow, RunCounts, RunStatus, Stored

KilledReason = Literal["wall", "signal", "output"]
KILL_WALL: KilledReason = "wall"
KILL_SIGNAL: KilledReason = "signal"
KILL_OUTPUT: KilledReason = "output"


# OpenAI routing (#191): a model is sent to /responses by prefix, or to /chat only if listed; config rejects the rest.
RESPONSES_MODELS = ("gpt-6", "gpt-5")  # reasoning families (gpt-5, -nano, -mini, gpt-5.x) served via /responses
CHAT_MODELS = frozenset({"gpt-4o-mini", "gpt-4.1-mini"})  # the only OpenAI models sent to /chat


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
    refused: bool = False  # the vendor declined to answer (safety classifier); omitted from the log when false
    reasoning_summary: str = (
        ""  # vendor-written summary of the thinking when it returns one (never raw chain of thought)
    )
    units: int | None = None  # engine-set arena units (Rules.accounting = "arena"); None: vendor tokens count

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def spent_tokens(self) -> int:
        """What stakes, the clock and wallets count: arena units when set, else vendor tokens."""
        return self.units if self.units is not None else self.total_tokens


class LLMProvider(Protocol):
    name: str

    def complete(self, request: CompletionRequest) -> Completion: ...


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    stdout: str
    exit_code: int
    timed_out: bool
    killed_reason: KilledReason | None = None


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


class ReplaySource(MatchStore, Protocol):
    def replays(self) -> list[tuple[str, list[Path]]]:
        """``(match_id, files)`` per stored match: absolute replay files only, never a secret or a symlinked match."""
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

    def reserve(
        self,
        match_id: str,
        model: str,
        usd: float,
        caps: Sequence[tuple[str, str, float]] = (),
        key: str | None = None,
    ) -> str:
        """Append a worst-case entry. Under one exclusive lock: refuse if any ``(label, prefix, max_usd)`` cap would
        pass, and replace an open reservation already holding ``key`` (one attempt, one reservation)."""
        ...

    def record(
        self,
        match_id: str,
        model: str,
        tokens: int,
        usd: float,
        settles: str | None = None,
        key: str | None = None,
    ) -> None:
        """Append a charge (with ``key``, the attempt's)."""
        ...

    def charged(self, key: str) -> bool:
        """True once a charge for the attempt ``key`` is in the ledger."""
        ...


class ReplyStore(Protocol):
    """Durable provider replies by attempt key, so a resume after a crash between an attempt's charge and its own
    event reuses the reply instead of paying again. Money lives in ``SpendLedger``; this holds only the text."""

    def keep(self, key: str, reply: dict[str, Any]) -> None:
        """Store the reply durably (before the charge line that refers to it)."""
        ...

    def get(self, key: str) -> dict[str, Any] | None:
        """The kept reply, or None."""
        ...

    def forget(self, key: str) -> None:
        """Drop the kept reply once the attempt's own event is durable."""
        ...


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


class Uploader(Protocol):
    """Copies local files to a private object-store prefix (``duel backup``); raises ``ConfigError`` on failure."""

    def upload(self, files: Sequence[Path], dest: str) -> None: ...
