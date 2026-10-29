"""Event sourcing: every change to a match is an event, and ``apply`` is the only way state changes.

The engine records an event and applies it; replay folds the same events from scratch
(ADR 0004). Event data holds plain JSON so the log is self-describing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import (
    AgentSpec,
    Attack,
    AttackAttempt,
    CapHit,
    Challenge,
    Event,
    Finding,
    Keystone,
    MatchState,
    Rules,
    SandboxLimits,
    Stage,
    Usage,
    Validation,
)
from claude_duel.domain.scoring import add_scores
from claude_duel.domain.stakes import keystone_commit
from claude_duel.domain.state_machine import recover, transition

MATCH_CREATED = "match_created"
STAGE_CHANGED = "stage_changed"
MATCH_FAILED = "match_failed"
MATCH_RECOVERED = "match_recovered"
MATCH_CAPPED = "match_capped"
CALL_CHARGED = "call_charged"
BUILD_REJECTED = "build_rejected"
CHALLENGE_BUILT = "challenge_built"
CHALLENGE_VALIDATED = "challenge_validated"
ATTACK_STARTED = "attack_started"
ATTACK_ATTEMPTED = "attack_attempted"
CHALLENGE_ATTACKED = "challenge_attacked"
ROUND_SCORED = "round_scored"
FINDING_FLAGGED = "finding_flagged"
NOTE_RECORDED = "note_recorded"
BUDGET_OVERRIDDEN = "budget_overridden"
STAKES_SET = "stakes_set"
KEYSTONE_SEALED = "keystone_sealed"
KEYSTONE_REVEALED = "keystone_revealed"
GHOST_RUN = "ghost_run"

# Event.v is the minimum reader version an event needs; readers refuse anything newer (#139).
# 0 = written before versioning; 1 = typed payloads; 2 = note_recorded, call token counts and the new
# Rules/Usage/AttackAttempt fields (a v1 build would reject them under extra="forbid").
SCHEMA_VERSION = 2


_M = TypeVar("_M", bound=BaseModel)


def typed(model: type[_M], data: object) -> _M:
    """Validate a series/tournament payload; a wrong shape means the log is corrupt (#139)."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise EventLogCorruptError(f"bad {model.__name__} payload ({exc.error_count()} errors)") from exc


def check_version(event: Event, newest: int, what: str = "event") -> None:
    """Refuse an event written for a newer reader than this build."""
    if event.v > newest:
        raise EventLogCorruptError(f"{what} {event.seq} uses schema v{event.v}; this build reads up to v{newest}")


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MatchCreated(_Payload):
    seed: str = ""  # absent before ADR 0007
    framing_text: str = ""  # absent before framings
    created_seq: int | None = None  # store-assigned creation order (#137); absent in older logs
    agents: tuple[AgentSpec, AgentSpec]
    rules: Rules
    limits: SandboxLimits


class StageChanged(_Payload):
    to: Stage


class MatchFailed(_Payload):
    reason: str


class MatchCapped(_Payload):
    hit: CapHit


class MatchRecovered(_Payload):
    pass


class CallCharged(_Payload):
    agent: str
    model: str
    tokens: int
    usd: float
    elapsed_ms: int = 0  # provider latency, measured once (absent before ADR 0007)
    input_tokens: int = 0  # split of ``tokens`` for the token clock (#36); absent in older logs
    output_tokens: int = 0
    reasoning_tokens: int = 0  # thinking/reasoning part of ``output_tokens`` (billed as output); absent before #145
    truncated: bool = False


class BuildRejected(_Payload):
    challenge_id: str
    error: str


class ChallengeBuilt(_Payload):
    challenge: Challenge


class ChallengeValidated(_Payload):
    validation: Validation


class AttackStarted(_Payload):
    """An attack attempt is about to be made (presentation only; absent from older logs)."""

    key: str
    try_no: int


class AttackAttempted(_Payload):
    key: str
    attempt: AttackAttempt


class ChallengeAttacked(_Payload):
    key: str
    attack: Attack


class BudgetOverridden(_Payload):
    """Audit record of a run-cap override (design-stakes-modes.md). It changes no match state."""

    field: str
    default: float
    value: float
    reason: str
    ceiling: float


class NoteRecorded(_Payload):
    """An agent's private scratchpad note (memory kind ``summary+scratchpad``), already flag-redacted."""

    agent: str
    round: int
    text: str


class StakesSet(_Payload):
    """An attacker's settled ledger stakes for one round; ``fallback`` = its answer was invalid (alloc_fallback)."""

    agent: str
    round: int
    stakes: tuple[int, ...]
    fallback: bool = False


class KeystoneSealed(_Payload):
    """An author's keystone commit only; the index is logged at reveal (KeystoneRevealed)."""

    agent: str
    round: int
    commit: str


class KeystoneRevealed(_Payload):
    agent: str
    round: int
    index: int | None = None
    salt: str


class GhostRun(_Payload):
    """One $0 sandbox replay of a past capture's payload; ``stdout_sha`` is of the redacted output."""

    round: int
    ghost_id: str
    target: str
    hit: bool
    stdout_sha: str


class RoundScored(_Payload):
    round: int
    delta: dict[str, int]


class FindingFlagged(_Payload):
    finding: Finding


def _flagged(state: MatchState, p: FindingFlagged) -> None:
    state.findings.append(p.finding)


def _created(state: MatchState, p: MatchCreated) -> None:
    return None  # fields are set by ``initial_state``


def _stage(state: MatchState, p: StageChanged) -> None:
    transition(state, p.to)


def _failed(state: MatchState, p: MatchFailed) -> None:
    transition(state, Stage.FAILED)
    state.failure = p.reason


def _capped(state: MatchState, p: MatchCapped) -> None:
    transition(state, Stage.CAPPED)
    state.capped = p.hit


def _recovered(state: MatchState, p: MatchRecovered) -> None:
    recover(state)


def _charged(state: MatchState, p: CallCharged) -> None:
    state.usage.calls += 1
    state.last_agent = p.agent
    state.usage.tokens += p.tokens
    state.usage.usd += p.usd
    state.usage.elapsed_ms += p.elapsed_ms
    mine = state.usage_by_agent.setdefault(p.agent, Usage())
    mine.calls += 1
    mine.tokens += p.tokens
    mine.usd += p.usd
    for u in (state.usage, mine):
        u.input_tokens += p.input_tokens
        u.output_tokens += p.output_tokens
        u.reasoning_tokens += p.reasoning_tokens
    clock = state.rules.clock
    split = p.output_tokens + p.input_tokens // clock.input_divisor
    mine.clock_tokens += split if (p.input_tokens or p.output_tokens) else p.tokens


def _rejected(state: MatchState, p: BuildRejected) -> None:
    state.build_errors.setdefault(p.challenge_id, []).append(p.error)


def _built(state: MatchState, p: ChallengeBuilt) -> None:
    state.challenges[p.challenge.id] = p.challenge


def _validated(state: MatchState, p: ChallengeValidated) -> None:
    state.validations[p.validation.challenge_id] = p.validation


def _started(state: MatchState, p: AttackStarted) -> None:
    return None  # no state change: replay of logs with or without it is identical


def _attempted(state: MatchState, p: AttackAttempted) -> None:
    state.attempts.setdefault(p.key, []).append(p.attempt)


def _attacked(state: MatchState, p: ChallengeAttacked) -> None:
    state.attacks[p.key] = p.attack


def _overridden(state: MatchState, p: BudgetOverridden) -> None:
    del state, p  # audit only


def _noted(state: MatchState, p: NoteRecorded) -> None:
    state.notes.setdefault(p.agent, {})[p.round] = p.text  # last note of the round wins


def _staked(state: MatchState, p: StakesSet) -> None:
    state.stakes[f"{p.round}:{p.agent}"] = list(p.stakes)


def _sealed(state: MatchState, p: KeystoneSealed) -> None:
    state.keystones[f"{p.round}:{p.agent}"] = Keystone(commit=p.commit)


def _revealed(state: MatchState, p: KeystoneRevealed) -> None:
    key = f"{p.round}:{p.agent}"
    sealed = state.keystones.get(key)
    if sealed is None or sealed.commit != keystone_commit(p.index, p.salt):
        raise EventLogCorruptError(f"tampered: keystone reveal for {key} does not match its commit")
    state.keystones[key] = sealed.model_copy(update={"index": p.index, "salt": p.salt, "revealed": True})


def _ghost_ran(state: MatchState, p: GhostRun) -> None:
    state.ghost_runs[f"{p.round}:{p.ghost_id}>{p.target}"] = p.hit


def _scored(state: MatchState, p: RoundScored) -> None:
    state.scores = add_scores(state.scores, p.delta)
    state.scored_rounds.append(p.round)


_HANDLERS: dict[str, tuple[type[_Payload], Callable[[MatchState, Any], None]]] = {
    MATCH_CREATED: (MatchCreated, _created),
    STAGE_CHANGED: (StageChanged, _stage),
    MATCH_FAILED: (MatchFailed, _failed),
    MATCH_RECOVERED: (MatchRecovered, _recovered),
    MATCH_CAPPED: (MatchCapped, _capped),
    CALL_CHARGED: (CallCharged, _charged),
    BUILD_REJECTED: (BuildRejected, _rejected),
    CHALLENGE_BUILT: (ChallengeBuilt, _built),
    CHALLENGE_VALIDATED: (ChallengeValidated, _validated),
    ATTACK_STARTED: (AttackStarted, _started),
    ATTACK_ATTEMPTED: (AttackAttempted, _attempted),
    CHALLENGE_ATTACKED: (ChallengeAttacked, _attacked),
    ROUND_SCORED: (RoundScored, _scored),
    FINDING_FLAGGED: (FindingFlagged, _flagged),
    NOTE_RECORDED: (NoteRecorded, _noted),
    BUDGET_OVERRIDDEN: (BudgetOverridden, _overridden),
    STAKES_SET: (StakesSet, _staked),
    KEYSTONE_SEALED: (KeystoneSealed, _sealed),
    KEYSTONE_REVEALED: (KeystoneRevealed, _revealed),
    GHOST_RUN: (GhostRun, _ghost_ran),
}


def payload(event: Event) -> _Payload:
    """The event's typed payload; a wrong shape, unknown type or newer schema means the log is corrupt."""
    check_version(event, SCHEMA_VERSION)
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
        "seed": state.seed,
        "framing_text": state.framing_text,
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
    return MatchState(
        match_id=match_id,
        seed=p.seed,
        framing_text=p.framing_text,
        created_seq=p.created_seq,
        agents=p.agents,
        rules=p.rules,
        limits=p.limits,
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
