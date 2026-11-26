"""Duplicate play (no-fluke ladder): a pure fold over ``<id>.duplicate.jsonl``.

A board is one seed played twice as two normal matches with the agent order swapped. Its difference is the first
agent's net points summed over both halves, clipped to +-``board_cap``. A reply truncated twice is the model's own
outcome and is scored like a refusal (a build forfeit, a spent attack try); it is counted per agent on the board but
never voids it or replays it: no board is ever void. The result is decided on boards only (never on spend): the
stop rule's test must back the winner, else it is inconclusive.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from claude_duel.domain import match_stats as ms
from claude_duel.domain.driver import fold_created
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.events import check_version, typed
from claude_duel.domain.models import AgentSpec, AttackAttempt, Event, MatchState, Rules, SandboxLimits

DUPLICATE_CREATED, BOARD_SCORED, DUPLICATE_CAPPED = "duplicate_created", "board_scored", "duplicate_capped"
DUPLICATE_SCHEMA_VERSION = 1  # Event.v of duplicate events, versioned apart from match events (#139)
MAX_DIFFICULTY = 3
MAX_BOARDS = 64  # money caps bind first; this only stops a typo
Result = Literal["first", "second", "inconclusive"]


SIGN_TEST_ALPHA = 0.05


class DuplicateRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    boards_min: int = Field(default=12, ge=1, le=MAX_BOARDS)
    boards_max: int = Field(default=48, ge=1, le=MAX_BOARDS)
    board_cap_mult: int = Field(default=2, ge=1, le=10, description="board clip = mult x max defense award")
    # "margin" (default): exact sign-flip test on the seat-paired board margins; stop early past boards_min only at
    # p <= early_alpha (checked every look_every boards), else call at boards_max with p <= margin_alpha. "sprt" is the
    # older win-count rule (sign test or SPRT), kept so logs that pinned it replay unchanged.
    stop: Literal["none", "sprt", "margin"] = "margin"
    sprt_p1: float = Field(default=0.75, gt=0.5, lt=1)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    beta: float = Field(default=0.05, gt=0, lt=0.5)
    margin_alpha: float = Field(default=0.035, gt=0, lt=0.5)
    early_alpha: float = Field(default=0.005, gt=0, lt=0.5)
    look_every: int = Field(default=2, ge=1, le=MAX_BOARDS)
    match_max_usd: float = Field(default=2.0, gt=0, description="checked against played boards before each board")

    @model_validator(mode="after")
    def _range(self) -> DuplicateRule:
        if self.boards_min > self.boards_max:
            raise ValueError("boards_min must be <= boards_max")
        return self


class Board(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    board_no: int
    seed: str  # the seed both halves were played on
    halves: tuple[str, str]  # match ids: first agent leads, then second agent leads
    raw: int  # first agent's net points over both halves
    diff: int  # raw clipped to +-board_cap
    truncated: dict[str, int] = Field(default_factory=dict)  # agent -> twice-truncated builds and tries (scored)
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
        return ms.record(self.diffs())

    def sprt(self) -> ms.Sprt:
        r = self.record()
        return ms.sprt(r.wins, r.losses, self.rule.sprt_p1, self.rule.alpha, self.rule.beta)

    def done(self) -> bool:
        return self.capped or stop_now(self.rule, len(self.boards), self.diffs())

    def p_value(self) -> float:
        r = self.record()
        return ms.sign_test_p(r.wins, r.losses)

    def margin_p(self) -> float:
        """Exact seat-paired sign-flip p on the summed board margin (what the margin rule calls on)."""
        return ms.sign_flip_p(self.diffs())

    def ci(self) -> tuple[float, float]:
        return ms.bootstrap_ci(self.diffs(), self.seed)

    def result(self) -> Result:
        """``first``/``second`` only when the sign test (p <= 0.05) or the SPRT backs it; spend never decides."""
        if not self.done():
            raise ValueError("duplicate match is not finished")
        return verdict(self.rule, self.diffs(), final=len(self.boards) >= self.rule.boards_max or self.capped)

    def winner(self) -> str | None:
        res = self.result()
        return self.agents[0].name if res == "first" else self.agents[1].name if res == "second" else None

    def spent_usd(self) -> float:
        return sum(b.usd for b in self.boards)

    def next_board(self) -> int:
        return len(self.boards) + 1

    def half_id(self, board_no: int, half: int) -> str:
        return f"{self.duplicate_id}-b{board_no}{'ab'[half]}"


def stop_now(rule: DuplicateRule, boards: int, diffs: list[int]) -> bool:
    """The stop rule after ``boards`` boards (``diffs``: their margins): boards_max, or an early call."""
    if boards >= rule.boards_max:
        return True
    if boards < rule.boards_min or rule.stop == "none":
        return False
    if rule.stop == "margin":
        return (boards - rule.boards_min) % rule.look_every == 0 and ms.sign_flip_p(diffs) <= rule.early_alpha
    r = ms.record(diffs)
    return ms.sprt(r.wins, r.losses, rule.sprt_p1, rule.alpha, rule.beta) != "continue"


def verdict(rule: DuplicateRule, diffs: list[int], *, final: bool = True) -> Result:
    """A side wins only when the test backs it; otherwise the match is too close to call ("inconclusive"). Margin:
    p <= margin_alpha at boards_max (or a cap), p <= early_alpha before."""
    if rule.stop == "margin":
        total = sum(diffs)
        if total and ms.sign_flip_p(diffs) <= (rule.margin_alpha if final else rule.early_alpha):
            return "first" if total > 0 else "second"
        return "inconclusive"
    r = ms.record(diffs)
    p, test = ms.sign_test_p(r.wins, r.losses), ms.sprt(r.wins, r.losses, rule.sprt_p1, rule.alpha, rule.beta)
    if r.wins > r.losses and (p <= SIGN_TEST_ALPHA or test == "first"):
        return "first"
    if r.losses > r.wins and (p <= SIGN_TEST_ALPHA or test == "second"):
        return "second"
    return "inconclusive"


def next_board_usd(state: DuplicateState) -> float:
    """Worst case for the next board: the dearest board so far (0 before the first, which the cap check skips)."""
    return max((b.usd for b in state.boards), default=0.0)


def board_seed(seed: str, board_no: int) -> str:
    return hashlib.sha256(f"{seed}:board:{board_no}".encode()).hexdigest()[:16]


def half_agents(agents: tuple[AgentSpec, AgentSpec], half: int) -> tuple[AgentSpec, AgentSpec]:
    a, b = agents
    return (a, b) if half == 0 else (b, a)


def board_cap(rule: DuplicateRule, rules: Rules) -> int:
    """Clip = mult x the largest one-challenge defense award (difficulty 3, at the largest round weight: the last
    round's number under linear weights, 1 under flat ones), so a multi-round mode is not clipped flat."""
    weight = rules.rounds if rules.scoring.round_weight == "linear" else 1
    return rule.board_cap_mult * rules.scoring.defense_points * MAX_DIFFICULTY * weight


def build_truncated(state: MatchState, challenge_id: str) -> bool:
    """A build forfeited on a twice-truncated reply (scored as a forfeit, counted per agent). Reads the engine-set
    ``BuildRejected.truncated`` (folded into ``state.build_truncated``), never a reason string."""
    ch = state.challenges.get(challenge_id)
    return ch is not None and ch.spec is None and challenge_id in state.build_truncated


def attack_truncated(attempt: AttackAttempt) -> bool:
    """An attack try whose reply was truncated twice (a spent try, counted per agent). Reads the engine-set
    ``truncated`` field, never the output text, which is the opponent's challenge stdout."""
    return attempt.truncated


def truncations(state: MatchState) -> Counter[str]:
    """Per agent: builds forfeited and attack tries spent on a twice-truncated reply in one half."""
    out: Counter[str] = Counter()
    for cid in state.validations:
        if build_truncated(state, cid):
            out[state.challenges[cid].author] += 1
    for key, tries in state.attempts.items():
        out[key.rsplit(":", 1)[-1]] += sum(attack_truncated(a) for a in tries)
    return +out  # drops agents with no truncation


def net(state: MatchState, name: str) -> int:
    scores = state.final_scores()
    return scores[name] - scores[state.opponent(name)]


def board_of(state: DuplicateState, board_no: int, seed: str, halves: tuple[MatchState, MatchState]) -> Board:
    """A board's record from its two finished halves: the first agent's net points, clipped."""
    raw = sum(net(h, state.agents[0].name) for h in halves)
    cut = sum((truncations(h) for h in halves), Counter[str]())
    cap = board_cap(state.rule, state.rules)
    return Board(
        board_no=board_no,
        seed=seed,
        halves=(halves[0].match_id, halves[1].match_id),
        raw=raw,
        diff=max(-cap, min(cap, raw)),
        truncated=dict(cut),
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
