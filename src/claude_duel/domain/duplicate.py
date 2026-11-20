"""Duplicate play (no-fluke ladder): a pure fold over ``<id>.duplicate.jsonl``.

A board is one seed played twice as two normal matches with the agent order swapped. Its difference is the first
agent's net points summed over both halves, clipped to +-``board_cap``. A harness fault (a reply truncated twice)
voids the board, which is replayed on ``replay_seed`` up to ``fault_replays`` times and then counts 0. The result is
decided on boards only (never on spend): the sign test or the SPRT must back the winner, else it is inconclusive.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from claude_duel.domain import match_stats as ms
from claude_duel.domain.driver import fold_created
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.events import check_version, typed
from claude_duel.domain.models import TRUNCATED, AgentSpec, AttackAttempt, Event, MatchState, Rules, SandboxLimits

DUPLICATE_CREATED, BOARD_SCORED, DUPLICATE_CAPPED = "duplicate_created", "board_scored", "duplicate_capped"
DUPLICATE_SCHEMA_VERSION = 1  # Event.v of duplicate events, versioned apart from match events (#139)
MAX_DIFFICULTY = 3
Result = Literal["first", "second", "inconclusive", "void"]


class DuplicateRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    boards_min: int = Field(default=4, ge=1, le=16)
    boards_max: int = Field(default=8, ge=1, le=16)
    board_cap_mult: int = Field(default=2, ge=1, le=10, description="board clip = mult x max defense award")
    fault_replays: int = Field(default=2, ge=0, le=2)
    void_share_max_pct: int = Field(default=25, ge=0, le=100, description="more void boards => inconclusive")
    stop: Literal["none", "sprt"] = "sprt"
    sprt_p1: float = Field(default=0.75, gt=0.5, lt=1)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    beta: float = Field(default=0.05, gt=0, lt=0.5)
    match_max_usd: float = Field(default=2.0, gt=0, description="checked against played boards before each board")

    @model_validator(mode="after")
    def _range(self) -> DuplicateRule:
        if self.boards_min > self.boards_max:
            raise ValueError("boards_min must be <= boards_max")
        return self


class Board(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    board_no: int
    seed: str  # the seed both halves were played on (the replay seed after a fault)
    halves: tuple[str, str]  # match ids: first agent leads, then second agent leads
    raw: int  # first agent's net points over both halves
    diff: int  # raw clipped to +-board_cap; 0 when void
    void: bool = False
    fault: str = ""  # why the last try was voided (empty when scored)
    replays: int = 0
    usd: float = 0.0


class DuplicateState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duplicate_id: str
    seed: str
    rule: DuplicateRule
    agents: tuple[AgentSpec, AgentSpec]
    rules: Rules
    limits: SandboxLimits
    boards: list[Board] = Field(default_factory=list)
    capped: bool = False  # the next board could pass match_max_usd: scored on the boards played (ADR 0014)

    def diffs(self) -> list[int]:
        return [b.diff for b in self.boards]

    def record(self) -> ms.Record:
        return ms.record([b.diff for b in self.boards if not b.void])

    def void_share_exceeded(self) -> bool:
        voids = sum(b.void for b in self.boards)
        return 100 * voids > self.rule.void_share_max_pct * max(1, len(self.boards))

    def sprt(self) -> ms.Sprt:
        r = self.record()
        return ms.sprt(r.wins, r.losses, self.rule.sprt_p1, self.rule.alpha, self.rule.beta)

    def done(self) -> bool:
        n = len(self.boards)
        if self.capped or n >= self.rule.boards_max:
            return True
        return n >= self.rule.boards_min and self.rule.stop == "sprt" and self.sprt() != "continue"

    def p_value(self) -> float:
        r = self.record()
        return ms.sign_test_p(r.wins, r.losses)

    def ci(self) -> tuple[float, float]:
        return ms.bootstrap_ci(self.diffs(), self.seed)

    def result(self) -> Result:
        """``first``/``second`` only when the sign test (p <= 0.05) or the SPRT backs it; spend never decides."""
        if not self.done():
            raise ValueError("duplicate match is not finished")
        if self.void_share_exceeded():
            return "void"
        r = self.record()
        test = self.sprt()
        if r.wins > r.losses and (self.p_value() <= 0.05 or test == "first"):
            return "first"
        if r.losses > r.wins and (self.p_value() <= 0.05 or test == "second"):
            return "second"
        return "inconclusive"

    def winner(self) -> str | None:
        res = self.result()
        return self.agents[0].name if res == "first" else self.agents[1].name if res == "second" else None

    def spent_usd(self) -> float:
        return sum(b.usd for b in self.boards)

    def next_board(self) -> int:
        return len(self.boards) + 1

    def half_id(self, board_no: int, half: int, replay: int = 0) -> str:
        return f"{self.duplicate_id}-b{board_no}{'ab'[half]}" + (f"-r{replay}" if replay else "")


def next_board_usd(state: DuplicateState) -> float:
    """Worst case for the next board: the dearest board so far (0 before the first, which the cap check skips)."""
    return max((b.usd for b in state.boards), default=0.0)


def board_seed(seed: str, board_no: int) -> str:
    return hashlib.sha256(f"{seed}:board:{board_no}".encode()).hexdigest()[:16]


def replay_seed(seed: str, replay: int) -> str:
    """The seed of a board's ``replay``-th retry after a harness fault (0 = the board seed itself)."""
    return seed if replay == 0 else hashlib.sha256(f"{seed}:replay:{replay}".encode()).hexdigest()[:16]


def half_agents(agents: tuple[AgentSpec, AgentSpec], half: int) -> tuple[AgentSpec, AgentSpec]:
    a, b = agents
    return (a, b) if half == 0 else (b, a)


def board_cap(rule: DuplicateRule, rules: Rules) -> int:
    """Clip = mult x the largest one-challenge defense award (difficulty 3, at the largest round weight: the last
    round's number under linear weights, 1 under flat ones), so a multi-round mode is not clipped flat."""
    weight = rules.rounds if rules.scoring.round_weight == "linear" else 1
    return rule.board_cap_mult * rules.scoring.defense_points * MAX_DIFFICULTY * weight


def build_fault(reason: str | None) -> bool:
    """A build forfeited on a twice-truncated reply is a harness fault; every other invalid reason is scored."""
    return (reason or "").startswith(f"malformed: {TRUNCATED}")


def attack_fault(attempt: AttackAttempt) -> bool:
    """An attack try whose reply was truncated twice is a harness fault; near misses and the rest are scored. Reads
    the engine-set ``truncated`` field, never the output text, which is the opponent's challenge stdout."""
    return attempt.truncated


def fault(state: MatchState) -> str:
    """The harness fault that voids a board, or "". Every other failure is the model's own and is scored."""
    if any(build_fault(v.reason) for v in state.validations.values()):
        return "build truncated"
    if any(attack_fault(a) for tries in state.attempts.values() for a in tries):
        return "attack truncated"
    return ""


def net(state: MatchState, name: str) -> int:
    scores = state.final_scores()
    return scores[name] - scores[state.opponent(name)]


def board_of(
    state: DuplicateState, board_no: int, seed: str, halves: tuple[MatchState, MatchState], replays: int
) -> Board:
    """A board's record from its two finished halves; a faulted pair is void (diff 0)."""
    why = next((f for f in map(fault, halves) if f), "")
    raw = sum(net(h, state.agents[0].name) for h in halves)
    cap = board_cap(state.rule, state.rules)
    return Board(
        board_no=board_no,
        seed=seed,
        halves=(halves[0].match_id, halves[1].match_id),
        raw=raw,
        diff=0 if why else max(-cap, min(cap, raw)),
        void=bool(why),
        fault=why,
        replays=replays,
        usd=sum(h.usage.usd for h in halves),
    )


def fold(duplicate_id: str, events: Iterable[Event]) -> DuplicateState:
    state, it = fold_created(
        events,
        created_type=DUPLICATE_CREATED,
        schema_version=DUPLICATE_SCHEMA_VERSION,
        what="duplicate",
        state_type=DuplicateState,
        id_field="duplicate_id",
        id_value=duplicate_id,
    )
    for e in it:
        check_version(e, DUPLICATE_SCHEMA_VERSION, "duplicate event")
        if e.type == DUPLICATE_CAPPED and not state.capped:
            state.capped = True
            continue
        if e.type != BOARD_SCORED or state.capped:
            raise EventLogCorruptError(f"unexpected duplicate event {e.type!r}")
        board = typed(Board, e.data)
        if board.board_no != state.next_board():
            raise EventLogCorruptError(f"board {board.board_no} recorded out of order")
        state.boards.append(board)
    return state
