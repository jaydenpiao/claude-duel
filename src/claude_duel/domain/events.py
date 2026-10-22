"""Event sourcing: every change to a match is an event, and ``apply`` is the only way state changes.

The engine records an event and applies it; replay folds the same events from scratch
(ADR 0004). Event data holds plain JSON so the log is self-describing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import (
    AgentSpec,
    Attack,
    AttackAttempt,
    Challenge,
    Event,
    MatchState,
    Rules,
    SandboxLimits,
    Stage,
    Validation,
)
from claude_duel.domain.scoring import add_scores
from claude_duel.domain.state_machine import recover, transition

MATCH_CREATED = "match_created"
STAGE_CHANGED = "stage_changed"
MATCH_FAILED = "match_failed"
MATCH_RECOVERED = "match_recovered"
CALL_CHARGED = "call_charged"
BUILD_REJECTED = "build_rejected"
CHALLENGE_BUILT = "challenge_built"
CHALLENGE_VALIDATED = "challenge_validated"
ATTACK_ATTEMPTED = "attack_attempted"
CHALLENGE_ATTACKED = "challenge_attacked"
ROUND_SCORED = "round_scored"

SCHEMA_VERSION = 1  # Event.v of new events; 0 = written before versioning (same payload shapes)


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MatchCreated(_Payload):
    agents: tuple[AgentSpec, AgentSpec]
    rules: Rules
    limits: SandboxLimits


class StageChanged(_Payload):
    to: Stage


class MatchFailed(_Payload):
    reason: str


class MatchRecovered(_Payload):
    pass


class CallCharged(_Payload):
    agent: str
    model: str
    tokens: int
    usd: float


class BuildRejected(_Payload):
    challenge_id: str
    error: str


class ChallengeBuilt(_Payload):
    challenge: Challenge


class ChallengeValidated(_Payload):
    validation: Validation


class AttackAttempted(_Payload):
    key: str
    attempt: AttackAttempt


class ChallengeAttacked(_Payload):
    key: str
    attack: Attack


class RoundScored(_Payload):
    round: int
    delta: dict[str, int]


def _created(state: MatchState, p: MatchCreated) -> None:
    return None  # fields are set by ``initial_state``


def _stage(state: MatchState, p: StageChanged) -> None:
    transition(state, p.to)


def _failed(state: MatchState, p: MatchFailed) -> None:
    transition(state, Stage.FAILED)
    state.failure = p.reason


def _recovered(state: MatchState, p: MatchRecovered) -> None:
    recover(state)


def _charged(state: MatchState, p: CallCharged) -> None:
    state.usage.calls += 1
    state.usage.tokens += p.tokens
    state.usage.usd += p.usd


def _rejected(state: MatchState, p: BuildRejected) -> None:
    state.build_errors.setdefault(p.challenge_id, []).append(p.error)


def _built(state: MatchState, p: ChallengeBuilt) -> None:
    state.challenges[p.challenge.id] = p.challenge


def _validated(state: MatchState, p: ChallengeValidated) -> None:
    state.validations[p.validation.challenge_id] = p.validation


def _attempted(state: MatchState, p: AttackAttempted) -> None:
    state.attempts.setdefault(p.key, []).append(p.attempt)


def _attacked(state: MatchState, p: ChallengeAttacked) -> None:
    state.attacks[p.key] = p.attack


def _scored(state: MatchState, p: RoundScored) -> None:
    state.scores = add_scores(state.scores, p.delta)
    state.scored_rounds.append(p.round)


_HANDLERS: dict[str, tuple[type[_Payload], Callable[[MatchState, Any], None]]] = {
    MATCH_CREATED: (MatchCreated, _created),
    STAGE_CHANGED: (StageChanged, _stage),
    MATCH_FAILED: (MatchFailed, _failed),
    MATCH_RECOVERED: (MatchRecovered, _recovered),
    CALL_CHARGED: (CallCharged, _charged),
    BUILD_REJECTED: (BuildRejected, _rejected),
    CHALLENGE_BUILT: (ChallengeBuilt, _built),
    CHALLENGE_VALIDATED: (ChallengeValidated, _validated),
    ATTACK_ATTEMPTED: (AttackAttempted, _attempted),
    CHALLENGE_ATTACKED: (ChallengeAttacked, _attacked),
    ROUND_SCORED: (RoundScored, _scored),
}


def payload(event: Event) -> _Payload:
    """The event's typed payload; a wrong shape, unknown type or newer schema means the log is corrupt."""
    if event.v > SCHEMA_VERSION:
        raise EventLogCorruptError(
            f"event {event.seq} uses schema v{event.v}; this build reads up to v{SCHEMA_VERSION}"
        )
    entry = _HANDLERS.get(event.type)
    if entry is None:
        raise EventLogCorruptError(f"unknown event type {event.type!r}")
    try:
        return entry[0].model_validate(event.data)
    except ValidationError as exc:
        raise EventLogCorruptError(
            f"event {event.seq} ({event.type}) has a bad payload: {exc.errors()[0]['msg']}"
        ) from exc


def created_data(state: MatchState) -> dict[str, object]:
    """Data for the first event. Never includes the match secret."""
    return {
        "agents": [a.model_dump(mode="json") for a in state.agents],
        "rules": state.rules.model_dump(mode="json"),
        "limits": state.limits.model_dump(mode="json"),
    }


def apply(state: MatchState, event: Event) -> None:
    if event.seq != state.last_seq + 1:
        raise EventLogCorruptError(f"event {event.seq} applied after {state.last_seq}")
    data = payload(event)
    _HANDLERS[event.type][1](state, data)
    state.last_seq = event.seq


def initial_state(match_id: str, created: Event) -> MatchState:
    """State described by a ``match_created`` event, before it is applied. The secret is not in the log."""
    if created.type != MATCH_CREATED:
        raise EventLogCorruptError("event log does not start with match_created")
    p = cast(MatchCreated, payload(created))
    return MatchState(match_id=match_id, secret="", agents=p.agents, rules=p.rules, limits=p.limits)


def fold(match_id: str, events: Iterable[Event]) -> MatchState:
    """Rebuild a match from its full event log."""
    it = iter(events)
    first = next(it, None)
    if first is None:
        raise EventLogCorruptError("event log is empty")
    state = initial_state(match_id, first)
    apply(state, first)
    for event in it:
        apply(state, event)
    return state
