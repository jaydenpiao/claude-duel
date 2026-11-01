"""Pure domain models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Stage(StrEnum):
    PENDING = "pending"
    BUILD = "build"
    VALIDATE = "validate"
    ATTACK = "attack"
    SCORE = "score"
    COMPLETE = "complete"
    CAPPED = "capped"  # a match-scoped cap was hit: terminal, void (ADR 0014)
    FAILED = "failed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Handicap(_Frozen):
    """Per-agent limit tweaks for cross-tier pairings (#38); pinned with the agents in ``match_created``."""

    attempts_delta: int = Field(default=0, ge=-49, le=49)  # added to rules.max_attempts (floor 1)
    token_budget_mult: float = Field(default=1.0, gt=0, le=10)  # scales rules.attack_token_budget
    bank_mult: float = Field(default=1.0, gt=0, le=10)  # scales rules.clock.bank_tokens
    score_offset: int = Field(default=0, ge=-1000, le=1000)  # added to this agent's match total for the result

    def is_set(self) -> bool:
        return self != Handicap()


class AgentSpec(_Frozen):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    provider: str = "fake"
    model: str | None = Field(default=None, description="None means the provider's configured default")
    skill: int = Field(default=2, ge=0, description="fake provider only")
    handicap: Handicap = Handicap()


class SandboxLimits(_Frozen):
    wall_seconds: float = Field(default=2.0, gt=0)
    cpu_seconds: int = Field(default=1, gt=0)
    memory_mb: int = Field(default=128, gt=0)
    max_output_bytes: int = Field(default=4096, gt=0)
    pids_limit: int = Field(default=64, gt=0)  # >= 64 so gVisor (runsc) can start
    cpus: float = Field(default=0.5, gt=0)


class ScoringRule(_Frozen):
    """Which pure scorer (``scoring.SCORERS``) and its parameters (ADR 0007)."""

    kind: Literal["classic", "efficiency", "timed", "ledger"] = "classic"
    solve_points: int = 10
    defense_points: int = 10  # >= solve_points: a defense is never worth less than a capture (ADR 0006)
    invalid_penalty: int = 5
    # efficiency (#35): a capture loses floor(max_penalty * tokens_used / reference_tokens) points, never below 1
    max_penalty: int = Field(default=10, ge=0)
    reference_tokens: int = Field(default=4000, ge=1)
    # efficiency unit (#35): "tokens" (same-vendor) or "usd" (cross-vendor: recorded micro-$)
    unit: Literal["tokens", "usd"] = "tokens"
    reference_usd_micros: int = Field(default=2000, ge=1)
    # ADR 0014: a CAPPED match is "void" (no winner, excluded from ratings) or, for casual play, "partial"
    # (a series counts it on the rounds already scored). Ratings always exclude CAPPED matches.
    on_cap: Literal["void", "partial"] = "void"
    # timed (#36): a capture loses min(full - 1, floor(max_penalty * min(1, tokens_used / par_tokens)))
    par_tokens: int = Field(default=8000, ge=1)


_LEGACY_SCORING = ("solve_points", "defense_points", "invalid_penalty")


class FramingRule(_Frozen):
    """The one paragraph of system text that frames the match (app/framings.py); nothing else differs."""

    kind: Literal["neutral", "notice", "competitive", "survival"] = "neutral"  # notice: neutral + rules sentence
    text_version: int = 1


class WalletRule(_Frozen):
    """Per-agent match-wide allowance (#35, mode ``capped``). It only tightens: the budget caps still apply."""

    per_agent_tokens: int | None = Field(default=None, ge=1)
    per_agent_usd: float | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, ge=16)  # per-call output cap, so one call can't drain a wallet


class ClockRule(_Frozen):
    """Token game clock (#36, mode ``blitz``): a per-agent bank of clock tokens plus a Fischer increment per call.

    Clock time is tokens, never wall time (lead decision), so flag falls replay exactly.
    """

    bank_tokens: int | None = Field(default=None, ge=1)
    increment_tokens: int = Field(default=0, ge=0)
    input_divisor: int = Field(default=4, ge=1)


class MemoryRule(_Frozen):
    """Between-round memory (#19): what each agent is shown about earlier rounds (domain/memory.py)."""

    kind: Literal["none", "summary", "summary+scratchpad"] = "none"
    max_chars: int = Field(default=3000, ge=200, le=20_000)  # about 750 tokens
    source_chars: int = Field(default=600, ge=0, le=5000)  # per opponent challenge


class LedgerRule(_Frozen):
    """Sealed Ledger stakes (design-core-logic.md): tokens an attacker splits across the opponent's challenges."""

    attack_tokens: int = Field(default=12_000, ge=1)  # per attacker per round
    min_alloc: int = Field(default=1_000, ge=1)  # each stake is 0 (skip) or at least this
    keystone: bool = Field(
        default=True, description="each author commits to one challenge: defended x2, captured -base//2"
    )
    terrain: Literal["none", "seeded"] = "seeded"  # a public stdin transform per challenge (domain/terrain.py)
    heat_window: int = Field(default=2, ge=0, le=20)  # own past rounds compared for novelty; 0 turns heat off
    heat_threshold_pct: int = Field(default=70, ge=1, le=100)  # pre-order AST LCS similarity at or over this -> heat
    heat_max_pct: int = Field(default=50, ge=0, le=100)  # heat penalty cap, % of that challenge's defense award


class ProphecyRule(_Frozen):
    """Prophecy menu tier (design-prophecy-wiring.md 5): sealed forecasts about the opponent's next round."""

    max_per_round: int = Field(default=2, ge=1, le=2)
    pool: int = Field(default=6, ge=1, le=20)  # per agent per match: total |swing| never exceeds it
    round_cap: int = Field(default=3, ge=1, le=6)  # also capped by the target's cheapest defense award
    rates_version: int = Field(default=0, ge=0)  # prophecy.PROPHECY_RATES key, so replay is deterministic


class DeepRule(_Frozen):
    """Deep layer (design-core-logic deep-layer doc), phase 1: Ghost Codex replay, no imports. Ledger only."""

    ghosts: bool = False  # replay past captures' payloads against the opponent's new challenges, $0 API
    max_ghost_runs: int = Field(default=24, ge=0, le=200)  # sandbox runs per round
    prophecy: ProphecyRule | None = None  # off by default


RULES_VERSION = 1  # bump when a Rules default changes meaning; logged with every match


class Rules(_Frozen):
    mode: str = Field(default="classic", description="preset name (domain/modes.py) this match started from")
    rules_version: int = RULES_VERSION
    rounds: int = Field(default=3, ge=1, le=20)
    challenges_per_round: int = Field(default=2, ge=1, le=10)
    max_attempts: int = Field(default=4, ge=1, le=50)
    early_bonus_attempts: int = Field(
        default=0, ge=0, le=10, description="extra tries in round 1, one fewer each round"
    )
    retry_hint: bool = Field(default=False, description="after a failed try, ask the attacker to trace check() (#95)")
    retry_feedback: bool = Field(
        default=False, description="check form: a failed try shows why check() said no, never its values (#100)"
    )
    attack_token_budget: int = Field(default=20_000, ge=1)
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default=None, description="thinking effort sent to both vendors (Claude output_config.effort, OpenAI reasoning)"
    )
    max_output_tokens: int = Field(
        default=1500, ge=64, le=64_000, description="per-call output cap, reasoning included; same for every agent"
    )
    match_deadline_s: int = Field(default=900, ge=1, description="match fails when recorded provider time passes it")
    challenge_form: Literal["program", "check"] = Field(
        default="program", description="program: authors write all I/O; check: authors write def check(s) only"
    )
    variety: bool = Field(default=False, description="themes, past designs and repeat check for authors (#79)")
    integrity_control: bool = Field(
        default=False, description="rerun every payload on a control that never prints the flag; a capture is a leak"
    )
    attacker_solver: bool = Field(
        default=False, description="attackers may send a sandboxed solver (throwaway flag); its stdout is the payload"
    )
    max_format_retries: int = Field(default=2, ge=0, le=5)
    max_source_bytes_base: int = Field(default=2048, ge=256)
    scoring: ScoringRule = ScoringRule()
    framing: FramingRule = FramingRule()
    memory: MemoryRule = MemoryRule()
    ledger: LedgerRule | None = Field(default=None, description="Sealed Ledger stakes (mode ledger); None = off")
    deep: DeepRule | None = Field(default=None, description="deep layer on top of ledger; None = off")
    wallet: WalletRule = WalletRule()
    clock: ClockRule = ClockRule()
    difficulty_gate: bool = Field(
        default=False, description="one cheap probe attack per build; a first-try solve is sent back to repair"
    )
    gate_mode: Literal["reject", "score"] = Field(
        default="reject", description="score: a probe-solved challenge stays valid but its defense pays half (#78)"
    )

    @model_validator(mode="before")
    @classmethod
    def _move_legacy_scoring(cls, data: Any) -> Any:
        """Pre-0007 configs and logged rules had the scoring numbers at top level; move them under ``scoring``."""
        if not isinstance(data, dict) or not any(k in data for k in _LEGACY_SCORING):
            return data
        data = dict(data)
        moved = {k: data.pop(k) for k in _LEGACY_SCORING if k in data}
        scoring = data.get("scoring")
        base = scoring.model_dump() if isinstance(scoring, ScoringRule) else dict(scoring or {})
        data["scoring"] = base | moved  # an explicit legacy override wins (e.g. over a preset's scoring)
        return data

    @model_validator(mode="after")
    def _ledger_k_fits(self) -> Rules:
        """Every challenge must be fundable at ``min_alloc`` (review-pr190: 1 <= K <= attack_tokens // min_alloc)."""
        if self.ledger is not None and self.challenges_per_round > self.ledger.attack_tokens // self.ledger.min_alloc:
            raise ValueError(
                f"ledger: {self.challenges_per_round} challenges per round cannot each get min_alloc "
                f"{self.ledger.min_alloc} from attack_tokens {self.ledger.attack_tokens}"
            )
        if self.deep is not None and self.ledger is None:
            raise ValueError("deep layer needs ledger")
        return self

    def max_source_bytes(self, round_no: int) -> int:
        """Escalation: authors get more room every round."""
        return self.max_source_bytes_base * round_no


class ChallengeSpec(_Frozen):
    """What an author submits."""

    title: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=2000)
    source: str = Field(min_length=1)
    reference_payload: str = Field(max_length=4096)
    solver: str = Field(
        default="",
        max_length=8192,
        description="optional program (same policy, no stdin) whose stdout becomes reference_payload; never shown",
    )
    difficulty: int = Field(ge=1, le=3)

    @model_validator(mode="before")
    @classmethod
    def _drop_arena_fields(cls, data: Any) -> Any:
        """#142: ``probe_solved`` is the arena's verdict (now on ``Challenge``); a reply that sends it is ignored."""
        if isinstance(data, dict) and "probe_solved" in data:
            data = {k: v for k, v in data.items() if k != "probe_solved"}
        return data


class Challenge(_Frozen):
    id: str
    round: int
    author: str
    spec: ChallengeSpec | None
    forfeit_reason: str | None = None
    flag_epoch: int = 0  # the repair try that built it; each try runs under its own flag (0 in older logs)
    probe_solved: bool = False  # set only by the arena's difficulty gate in gate_mode "score"; halves the defense award

    @model_validator(mode="before")
    @classmethod
    def _lift_probe_solved(cls, data: Any) -> Any:
        """Older logs kept ``probe_solved`` inside ``spec``; lift it onto the challenge (#142)."""
        if isinstance(data, dict) and isinstance(spec := data.get("spec"), dict) and "probe_solved" in spec:
            data = dict(data)
            data["spec"] = {k: v for k, v in spec.items() if k != "probe_solved"}
            data.setdefault("probe_solved", spec["probe_solved"])
        return data


class Validation(_Frozen):
    challenge_id: str
    valid: bool
    reason: str


CAPTURED = "<flag captured>"  # attempt output when the flag was revealed (the flag itself is never stored)


class AttackAttempt(_Frozen):
    """One paid attack attempt, checkpointed so resume never re-pays it."""

    payload: str
    output: str
    tokens: int
    usd_micros: int = 0  # round(usd * 1e6) charged at call time; the recorded price pin (0 in pre-#35 logs)

    @property
    def captured(self) -> bool:
        return self.output == CAPTURED


class Attack(_Frozen):
    challenge_id: str
    attacker: str
    solved: bool
    attempts: int
    tokens_used: int
    reason: str
    usd_micros: int = 0
    stake: int | None = Field(default=None, ge=0)  # ledger mode: tokens the attacker allocated here (0 = skipped)
    stake_overshoot: int = Field(default=0, ge=0)  # ledger: tokens past the stake (input estimate gap only)
    ghost: str | None = None  # deep: id of the ghost that captured this with no live attack (stake refunded)

    @classmethod
    def of(cls, challenge_id: str, attacker: str, history: list[AttackAttempt], *, solved: bool, reason: str) -> Attack:
        return cls(
            challenge_id=challenge_id,
            attacker=attacker,
            solved=solved,
            attempts=len(history),
            tokens_used=sum(a.tokens for a in history),
            usd_micros=sum(a.usd_micros for a in history),
            reason=reason,
        )


class Usage(BaseModel):
    """Cumulative provider usage for one match (persisted, survives resume)."""

    calls: int = 0
    tokens: int = 0
    usd: float = 0.0
    input_tokens: int = 0  # split of ``tokens`` (0 for logs before the split was recorded)
    output_tokens: int = 0
    reasoning_tokens: int = 0  # part of output_tokens: thinking/reasoning, billed as output
    clock_tokens: int = 0  # token-clock charge (#36): output + input // clock.input_divisor, per agent
    elapsed_ms: int = 0  # recorded provider time; the match deadline is measured on it, so resume keeps it


class CapHit(_Frozen):
    """Why a match ended ``CAPPED`` (ADR 0014)."""

    limit: Literal["match_usd", "match_tokens", "match_calls"]
    spent: float
    cap: float
    breaching_agent: str | None  # whose charged call took usage to the cap (None: cap reached before any call)
    stage: Stage
    round: int


class Finding(_Frozen):
    """A detector hit (domain/findings.py). Logged for review; never changes scores."""

    agent: str
    round: int
    category: str
    severity: str
    rule_id: str
    excerpt: str


class Event(_Frozen):
    v: int = Field(default=0, ge=0, description="payload schema version (events.SCHEMA_VERSION); 0 = pre-versioning")
    seq: int
    type: str
    trace_id: str
    data: dict[str, object]


class Keystone(_Frozen):
    """An author's committed keystone (design-core-logic.md 4.2): ``commit = sha256("<index>:<salt>")``.

    ``index`` is positional over the author's own challenges of the round, sorted by id; None = forfeited (no or
    invalid pick); it stays None until ``revealed``, since the seal event logs only the commit. The commit is
    integrity-only (the log can't be rewritten after the fact); the index is recoverable from the seeded salt.
    """

    commit: str
    index: int | None = None
    salt: str = ""  # set at reveal
    revealed: bool = False


class ProphecySeal(_Frozen):
    """One sealed prophecy; template, stake and salt stay unset until revealed (only the commit is logged first)."""

    commit: str
    template: str | None = None
    stake: int | None = None
    salt: str = ""
    revealed: bool = False


class MatchState(BaseModel):
    """Mutable aggregate; persisted after every unit of work."""

    model_config = ConfigDict(extra="forbid")

    match_id: str
    secret: str = Field(default="", repr=False, exclude=True)  # never serialized; stores keep it in its own file
    framing_text: str = ""  # pinned framing paragraph from match_created ("" = neutral)
    findings: list[Finding] = Field(default_factory=list)
    seed: str = ""  # deterministic tie-break/pairing seed, logged in match_created ("" in pre-0007 logs)
    created_seq: int | None = None  # store-assigned in match_created; orders Elo (#137). None in older logs
    agents: tuple[AgentSpec, AgentSpec]
    rules: Rules
    limits: SandboxLimits
    stage: Stage = Stage.PENDING
    round: int = 0
    challenges: dict[str, Challenge] = Field(default_factory=dict)
    stakes: dict[str, list[int]] = Field(default_factory=dict)  # "<round>:<attacker>" -> stakes (ledger mode)
    keystones: dict[str, Keystone] = Field(default_factory=dict)  # "<round>:<author>" -> sealed/revealed keystone
    terrains: dict[str, str] = Field(default_factory=dict)  # challenge id -> terrain (ledger mode)
    validations: dict[str, Validation] = Field(default_factory=dict)
    attacks: dict[str, Attack] = Field(default_factory=dict)
    scores: dict[str, int] = Field(default_factory=dict)
    scored_rounds: list[int] = Field(default_factory=list)
    attempts: dict[str, list[AttackAttempt]] = Field(default_factory=dict)
    ghost_runs: dict[str, bool] = Field(default_factory=dict)  # "<round>:<ghost id>><challenge id>" -> hit
    prophecies: dict[str, ProphecySeal] = Field(default_factory=dict)  # "<target round>:<agent>:<slot>" -> seal
    prophecy_points: dict[str, int] = Field(default_factory=dict)  # agent -> settled prophecy total (>= 0)
    prophecy_swing: dict[str, int] = Field(default_factory=dict)  # agent -> sum of |settled delta| (<= pool)
    build_errors: dict[str, list[str]] = Field(default_factory=dict)
    last_seq: int = -1
    failure: str | None = None
    failed_stage: Stage | None = None
    capped: CapHit | None = None
    last_agent: str | None = None  # agent of the latest charged call (the breacher if the next call is capped)
    usage: Usage = Field(default_factory=Usage)
    usage_by_agent: dict[str, Usage] = Field(default_factory=dict)  # folded from call_charged.agent (wallets)
    notes: dict[str, dict[int, str]] = Field(default_factory=dict)  # agent -> round -> private scratchpad note

    def handicap(self, name: str) -> Handicap:
        return next((a.handicap for a in self.agents if a.name == name), Handicap())

    def max_attempts(self, name: str) -> int:
        bonus = max(0, self.rules.early_bonus_attempts - (max(self.round, 1) - 1))
        return max(1, self.rules.max_attempts + bonus + self.handicap(name).attempts_delta)

    def attack_token_budget(self, name: str) -> int:
        return max(1, int(self.rules.attack_token_budget * self.handicap(name).token_budget_mult))

    def final_scores(self) -> dict[str, int]:
        """Totals plus each agent's ``score_offset``: what decides the result (raw ``scores`` stay replayable)."""
        return {a.name: self.scores.get(a.name, 0) + a.handicap.score_offset for a in self.agents}

    def opponent(self, name: str) -> str:
        a, b = self.agents
        return b.name if name == a.name else a.name

    def round_challenges(self, round_no: int) -> list[Challenge]:
        return [c for c in self.challenges.values() if c.round == round_no]
