"""No-fluke ladder (design-no-fluke §4.1): a pure fold over ``<id>.ladder.jsonl``.

Each step is one duplicate match between side a's and side b's current models. The loser steps down its list; an
step with no backed winner moves both sides down. The ladder ends when either list runs out, or when the next step's
worst case could pass ``ladder_max_usd`` (capped).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from claude_duel.domain.driver import fold_created
from claude_duel.domain.duplicate import Result
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.events import check_version, typed
from claude_duel.domain.models import Event

LADDER_CREATED, STEP_FINISHED, LADDER_CAPPED = "ladder_created", "step_finished", "ladder_capped"
LADDER_SCHEMA_VERSION = 1  # Event.v of ladder events, versioned apart from match events
# A pairing a vendor policy block (OpenAI cyber_policy) stopped: no verdict, excluded from claims; the ladder goes on.
StepResult = Result | Literal["blocked"]


class Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    step_no: int = Field(ge=1)
    a: str
    b: str
    duplicate_id: str
    result: StepResult  # "first" = side a won
    usd: float = Field(ge=0)


def advance(tiers: tuple[int, int], result: StepResult) -> tuple[int, int]:
    """The loser steps down; no backed winner (inconclusive, blocked, capped) moves both sides down (a
    ``capped`` step also ends the ladder)."""
    i, j = tiers
    if result == "first":
        return i, j + 1
    if result == "second":
        return i + 1, j
    return i + 1, j + 1


class LadderState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ladder_id: str
    config: dict[str, Any]  # the resolved plan, as logged
    digest: str  # sha256 of the canonical plan JSON
    a: tuple[str, ...]
    b: tuple[str, ...]
    start: tuple[int, int]
    steps: list[Step] = Field(default_factory=list)
    capped: bool = False

    def tiers(self) -> tuple[int, int]:
        tiers = self.start
        for s in self.steps:
            tiers = advance(tiers, s.result)
        return tiers

    def done(self) -> bool:
        i, j = self.tiers()
        return self.capped or i >= len(self.a) or j >= len(self.b)

    def next_pair(self) -> tuple[str, str]:
        i, j = self.tiers()
        return self.a[i], self.b[j]

    def step_id(self, step_no: int) -> str:
        return f"{self.ladder_id}-s{step_no}"

    def spent_usd(self) -> float:
        return sum(s.usd for s in self.steps)


def fold(ladder_id: str, events: Iterable[Event]) -> LadderState:
    state, it = fold_created(
        events,
        created_type=LADDER_CREATED,
        schema_version=LADDER_SCHEMA_VERSION,
        what="ladder",
        state_type=LadderState,
        id_field="ladder_id",
        id_value=ladder_id,
    )
    for e in it:
        check_version(e, LADDER_SCHEMA_VERSION, "ladder event")
        if e.type == LADDER_CAPPED and not state.capped:
            state.capped = True
            continue
        if e.type != STEP_FINISHED or state.done():
            raise EventLogCorruptError(f"unexpected ladder event {e.type!r}")
        step = typed(Step, e.data)
        if step.step_no != len(state.steps) + 1 or (step.a, step.b) != state.next_pair():
            raise EventLogCorruptError(f"ladder step {step.step_no} does not follow the ladder")
        state.steps.append(step)
    return state
