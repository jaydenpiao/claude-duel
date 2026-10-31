"""Typed public projection of one verified match, as ``duel sync`` hands it to a ``ResultsStore`` (ADR 0012).

Pure data: the app builds it from a verified chain; each adapter maps it to its own tables. Every field here has
already passed ``domain/public.py`` (event data) or ``flags.scrub`` (free text).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RunStatus = Literal["ok", "halted", "failed"]


@dataclass(frozen=True)
class MatchRow:
    match_id: str
    head_hash: str
    event_count: int
    stage: str
    round: int
    mode: str
    challenge_form: str
    framing: str
    rules: dict[str, object]
    limits: dict[str, object]
    failure: str | None
    projection_version: int


@dataclass(frozen=True)
class EntrantRow:
    entrant_id: str
    provider: str
    model: str
    is_fake: bool


@dataclass(frozen=True)
class AgentRow:
    match_id: str
    agent_name: str
    entrant_id: str
    handicap: dict[str, object]


@dataclass(frozen=True)
class ResultRow:
    match_id: str
    winner: str | None
    scores: dict[str, int]
    calls: int
    tokens: int
    usd: float


@dataclass(frozen=True)
class EventRow:
    event_hash: str
    prev_hash: str
    match_id: str
    seq: int
    kind: str
    v: int
    data: dict[str, object]


@dataclass(frozen=True)
class CallRow:
    event_hash: str
    match_id: str
    agent: str
    model: str
    tokens: int
    input_tokens: int
    output_tokens: int
    usd: float
    elapsed_ms: int


@dataclass(frozen=True)
class AttackRow:
    event_hash: str
    match_id: str
    challenge_id: str
    round: int
    author: str
    attacker: str
    solved: bool
    attempts: int
    tokens_used: int
    usd_micros: int


@dataclass(frozen=True)
class ScoreRow:
    event_hash: str
    match_id: str
    round: int
    agent: str
    points: int


@dataclass(frozen=True)
class RatingRow:
    entrant_id: str
    games: int
    elo: float
    bt: float
    bt_lo: float
    bt_hi: float
    elo_order: str
    elo_k: float
    through_match: str


@dataclass(frozen=True)
class MatchProjection:
    """Everything stored for one match; a store writes it atomically or not at all."""

    match: MatchRow
    entrants: tuple[EntrantRow, ...]
    agents: tuple[AgentRow, ...]
    result: ResultRow
    events: tuple[EventRow, ...]
    calls: tuple[CallRow, ...] = ()
    attacks: tuple[AttackRow, ...] = ()
    scores: tuple[ScoreRow, ...] = ()


@dataclass(frozen=True)
class Stored:
    """What a store already holds for one match: its event hashes and how it was projected."""

    hashes: frozenset[str] = field(default_factory=frozenset)
    projection_version: int | None = None
    rejected: bool = False


@dataclass
class RunCounts:
    inserted: int = 0  # matches with new events (or re-projected)
    skipped: int = 0  # matches already up to date
    rejected: int = 0
