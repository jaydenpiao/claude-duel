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
}
TERMINAL = frozenset({Stage.COMPLETE, Stage.FAILED})


def next_stage(state: MatchState) -> Stage:
    """The successor of the current stage under the rules."""
    if state.stage is Stage.SCORE:
        return Stage.COMPLETE if state.round >= state.rules.rounds else Stage.BUILD
    for target in _ALLOWED[state.stage]:
        return target
    raise InvalidTransitionError(f"{state.stage} is terminal")


def transition(state: MatchState, target: Stage) -> None:
    if target is Stage.FAILED and state.stage not in TERMINAL:
        state.stage = target
        return
    if target not in _ALLOWED[state.stage]:
        raise InvalidTransitionError(f"{state.stage} -> {target}")
    if target is Stage.BUILD:
        state.round += 1
    state.stage = target
