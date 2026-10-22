"""Pure domain models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Stage(StrEnum):
    PENDING = "pending"
    BUILD = "build"
    VALIDATE = "validate"
    ATTACK = "attack"
    SCORE = "score"
    COMPLETE = "complete"
    FAILED = "failed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentSpec(_Frozen):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    provider: str = "fake"
    model: str | None = Field(default=None, description="None means the provider's configured default")
    skill: int = Field(default=2, ge=0, description="fake provider only")


class SandboxLimits(_Frozen):
    wall_seconds: float = Field(default=2.0, gt=0)
    cpu_seconds: int = Field(default=1, gt=0)
    memory_mb: int = Field(default=128, gt=0)
    max_output_bytes: int = Field(default=4096, gt=0)
    pids_limit: int = Field(default=32, gt=0)
    cpus: float = Field(default=0.5, gt=0)


class Rules(_Frozen):
    rounds: int = Field(default=3, ge=1, le=20)
    challenges_per_round: int = Field(default=2, ge=1, le=10)
    max_attempts: int = Field(default=4, ge=1, le=50)
    attack_token_budget: int = Field(default=20_000, ge=1)
    max_format_retries: int = Field(default=2, ge=0, le=5)
    max_source_bytes_base: int = Field(default=2048, ge=256)
    solve_points: int = 10
    defense_points: int = 6
    invalid_penalty: int = 5

    def max_source_bytes(self, round_no: int) -> int:
        """Escalation: authors get more room every round."""
        return self.max_source_bytes_base * round_no


class ChallengeSpec(_Frozen):
    """What an author submits."""

    title: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=2000)
    source: str = Field(min_length=1)
    reference_payload: str = Field(max_length=4096)
    difficulty: int = Field(ge=1, le=3)


class Challenge(_Frozen):
    id: str
    round: int
    author: str
    spec: ChallengeSpec | None
    forfeit_reason: str | None = None


class Validation(_Frozen):
    challenge_id: str
    valid: bool
    reason: str


class Attack(_Frozen):
    challenge_id: str
    attacker: str
    solved: bool
    attempts: int
    tokens_used: int
    reason: str


class Usage(BaseModel):
    """Cumulative provider usage for one match (persisted, survives resume)."""

    calls: int = 0
    tokens: int = 0
    usd: float = 0.0


class Event(_Frozen):
    seq: int
    type: str
    trace_id: str
    data: dict[str, object]


class MatchState(BaseModel):
    """Mutable aggregate; persisted after every unit of work."""

    model_config = ConfigDict(extra="forbid")

    match_id: str
    secret: str
    agents: tuple[AgentSpec, AgentSpec]
    rules: Rules
    limits: SandboxLimits
    stage: Stage = Stage.PENDING
    round: int = 0
    challenges: dict[str, Challenge] = Field(default_factory=dict)
    validations: dict[str, Validation] = Field(default_factory=dict)
    attacks: dict[str, Attack] = Field(default_factory=dict)
    scores: dict[str, int] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)
    failure: str | None = None
    failed_stage: Stage | None = None
    usage: Usage = Field(default_factory=Usage)

    def opponent(self, name: str) -> str:
        a, b = self.agents
        return b.name if name == a.name else a.name

    def round_challenges(self, round_no: int) -> list[Challenge]:
        return [c for c in self.challenges.values() if c.round == round_no]
