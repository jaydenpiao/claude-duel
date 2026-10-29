"""Explicit match lifecycle."""

from claude_duel.domain.errors import InvalidTransitionError
from claude_duel.domain.models import MatchState, Stage

_ALLOWED: dict[Stage, frozenset[Stage]] = {
    Stage.PENDING: frozenset({Stage.BUILD}),
    Stage.BUILD: frozenset({Stage.VALIDATE}),
    Stage.VALIDATE: frozenset({Stage.ATTACK}),
    Stage.ATTACK: frozenset({Stage.SCORE}),
    Stage.SCORE: frozenset({Stage.BUILD, Stage.COMPLETE}),
    Stage.COMPLETE: frozenset(),
    Stage.FAILED: frozenset(),
    Stage.CAPPED: frozenset(),
}
TERMINAL = frozenset({Stage.COMPLETE, Stage.FAILED, Stage.CAPPED})
# The single successor of each linear stage; SCORE branches on the round.
_NEXT: dict[Stage, Stage] = {
    Stage.PENDING: Stage.BUILD,
    Stage.BUILD: Stage.VALIDATE,
    Stage.VALIDATE: Stage.ATTACK,
    Stage.ATTACK: Stage.SCORE,
}


def next_stage(state: MatchState) -> Stage:
    """The successor of the current stage under the rules."""
    if state.stage is Stage.SCORE:
        return Stage.COMPLETE if state.round >= state.rules.rounds else Stage.BUILD
    if state.stage not in _NEXT:
        raise InvalidTransitionError(f"{state.stage} is terminal")
    return _NEXT[state.stage]


def transition(state: MatchState, target: Stage) -> None:
    if target is Stage.CAPPED and state.stage not in TERMINAL:
        state.stage = target
        return
    if target is Stage.FAILED and state.stage not in TERMINAL:
        state.failed_stage = state.stage
        state.stage = target
        return
    if target not in _ALLOWED[state.stage]:
        raise InvalidTransitionError(f"{state.stage} -> {target}")
    if target is Stage.BUILD:
        state.round += 1
    state.stage = target


def recover(state: MatchState) -> None:
    """Re-open a failed match at the stage it failed in (work units are idempotent)."""
    if state.stage is not Stage.FAILED or state.failed_stage is None:
        raise InvalidTransitionError(f"cannot recover from {state.stage}")
    state.stage, state.failed_stage, state.failure = state.failed_stage, None, None
