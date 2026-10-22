"""Event sourcing: every change to a match is an event, and ``apply`` is the only way state changes.

The engine records an event and applies it; replay folds the same events from scratch
(ADR 0004). Event data holds plain JSON so the log is self-describing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import cast

from pydantic import TypeAdapter

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

_SCORES = TypeAdapter(dict[str, int])
Data = Mapping[str, object]


def _str(data: Data, key: str) -> str:
    return str(data[key])


def _int(data: Data, key: str) -> int:
    return int(cast(int, data[key]))


def _created(state: MatchState, data: Data) -> None:
    return None  # fields are set by ``initial_state``


def _stage(state: MatchState, data: Data) -> None:
    transition(state, Stage(_str(data, "to")))


def _failed(state: MatchState, data: Data) -> None:
    transition(state, Stage.FAILED)
    state.failure = _str(data, "reason")


def _recovered(state: MatchState, data: Data) -> None:
    recover(state)


def _charged(state: MatchState, data: Data) -> None:
    state.usage.calls += 1
    state.usage.tokens += _int(data, "tokens")
    state.usage.usd += float(cast(float, data["usd"]))


def _rejected(state: MatchState, data: Data) -> None:
    state.build_errors.setdefault(_str(data, "challenge_id"), []).append(_str(data, "error"))


def _built(state: MatchState, data: Data) -> None:
    ch = Challenge.model_validate(data["challenge"])
    state.challenges[ch.id] = ch


def _validated(state: MatchState, data: Data) -> None:
    v = Validation.model_validate(data["validation"])
    state.validations[v.challenge_id] = v


def _attempted(state: MatchState, data: Data) -> None:
    state.attempts.setdefault(_str(data, "key"), []).append(AttackAttempt.model_validate(data["attempt"]))


def _attacked(state: MatchState, data: Data) -> None:
    state.attacks[_str(data, "key")] = Attack.model_validate(data["attack"])


def _scored(state: MatchState, data: Data) -> None:
    state.scores = add_scores(state.scores, _SCORES.validate_python(data["delta"]))
    state.scored_rounds.append(_int(data, "round"))


_HANDLERS: dict[str, Callable[[MatchState, Data], None]] = {
    MATCH_CREATED: _created,
    STAGE_CHANGED: _stage,
    MATCH_FAILED: _failed,
    MATCH_RECOVERED: _recovered,
    CALL_CHARGED: _charged,
    BUILD_REJECTED: _rejected,
    CHALLENGE_BUILT: _built,
    CHALLENGE_VALIDATED: _validated,
    ATTACK_ATTEMPTED: _attempted,
    CHALLENGE_ATTACKED: _attacked,
    ROUND_SCORED: _scored,
}


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
    handler = _HANDLERS.get(event.type)
    if handler is None:
        raise EventLogCorruptError(f"unknown event type {event.type!r}")
    handler(state, event.data)
    state.last_seq = event.seq


def initial_state(match_id: str, created: Event) -> MatchState:
    """State described by a ``match_created`` event, before it is applied. The secret is not in the log."""
    if created.type != MATCH_CREATED:
        raise EventLogCorruptError("event log does not start with match_created")
    a, b = (AgentSpec.model_validate(x) for x in cast(list[object], created.data["agents"]))
    return MatchState(
        match_id=match_id,
        secret="",
        agents=(a, b),
        rules=Rules.model_validate(created.data["rules"]),
        limits=SandboxLimits.model_validate(created.data["limits"]),
    )


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
