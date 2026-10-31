"""Cost circuit breaker.

Caps: per match (persisted in state), per run (this process only), and lifetime (the shared
ledger, re-read before every call so concurrent processes see each other's spend). Concurrent
processes can each overshoot the lifetime cap by at most one in-flight call.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from claude_duel.adapters.providers.registry import VENDORS
from claude_duel.domain.errors import BudgetExceededError, ConfigError, MatchCapError
from claude_duel.domain.models import MatchState
from claude_duel.ports import Completion, CompletionRequest, SpendLedger

# USD per 1M tokens (input, output). This is also the model allowlist (low/medium-tier only). It is code-defined
# and deliberately not configurable: a config that could add models or lower prices would bypass the spend caps.
FAKE_MODEL = "fake-1"
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    FAKE_MODEL: (0.0, 0.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1-mini": (0.40, 1.60),
    # Current lineups, standard (non-batch, short-context, global) list prices as of 2026-10-22:
    # https://docs.anthropic.com/en/docs/about-claude/pricing and https://platform.openai.com/docs/pricing
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100k tokens; arena prompts are far below that
    "gpt-6-luna": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "claude-fable-5-1": (10.00, 50.00),
    "gpt-6-astra": (10.00, 50.00),
}
# Top-tier models (#10): priced and allowlisted, but refused unless a config sets ``allow_premium = true``,
# so a typo or a default can never start a costly match. The spend caps still apply on top.
PREMIUM: frozenset[str] = frozenset(
    {"claude-sonnet-5-5", "gpt-6.1-sol", "claude-opus-5-5", "gpt-5.6-sol", "claude-fable-5-1", "gpt-6-astra"}
)


def vendor_of(model: str) -> str:
    if model == FAKE_MODEL:
        return "fake"
    return next((v.name for v in VENDORS.values() if model.startswith(v.model_prefix)), "unknown")


def check_model_allowed(model: str, allow_premium: bool = False) -> None:
    if model not in DEFAULT_PRICES:
        raise ConfigError(f"model {model!r} is not in the allowlist: {sorted(DEFAULT_PRICES)}")
    if model in PREMIUM and not allow_premium:
        raise ConfigError(f"model {model!r} is premium; set allow_premium = true in the config to use it")


class BudgetLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    match_max_calls: int = Field(default=80, ge=1)
    match_max_tokens: int = Field(default=150_000, ge=1)
    match_max_usd: float = Field(default=0.25, gt=0)
    run_max_calls: int = Field(default=200, ge=1)
    run_max_usd: float = Field(default=0.50, gt=0)
    lifetime_max_usd: float = Field(default=33.0, gt=0)
    key_limit_usd: float = Field(default=50.0, gt=0, description="key's hard limit; must be >= lifetime_max_usd")

    @model_validator(mode="after")
    def _ceiling_under_key_limit(self) -> BudgetLimits:
        if self.lifetime_max_usd > self.key_limit_usd:
            raise ValueError("lifetime_max_usd must not exceed key_limit_usd")
        return self


RUN_OVERRIDE_CEILING_USD = 2.00  # hard top for ``--run-max-usd``; code-defined, not configurable


class RunOverride(BaseModel):
    """A per-invocation raise of ``run_max_usd`` (``duel experiment``); defaults never change."""

    model_config = ConfigDict(frozen=True)
    field: str = "run_max_usd"
    default: float
    value: float
    reason: str
    ceiling: float = RUN_OVERRIDE_CEILING_USD


def make_override(limits: BudgetLimits, field: str, value: float, reason: str, spent: float) -> RunOverride:
    """Check every guard rail; raise ``ConfigError`` before any call is made."""
    if field != "run_max_usd":
        raise ConfigError(f"budget override: only run_max_usd may be overridden, not {field!r}")
    if not reason.strip():
        raise ConfigError("budget override: --override-reason is required")
    if not math.isfinite(value):  # NaN would pass every comparison below and disable the run cap
        raise ConfigError(f"budget override: {value!r} is not a finite amount")
    if value > RUN_OVERRIDE_CEILING_USD:
        raise ConfigError(f"budget override: {value:.2f} exceeds the ceiling {RUN_OVERRIDE_CEILING_USD:.2f}")
    if value < limits.run_max_usd:
        raise ConfigError(f"budget override: {value:.2f} is below the default {limits.run_max_usd:.2f}")
    if spent + value > limits.lifetime_max_usd:
        raise ConfigError(
            f"budget override: lifetime spent ${spent:.2f} + {value:.2f} exceeds ${limits.lifetime_max_usd:.2f}"
        )
    return RunOverride(field=field, default=limits.run_max_usd, value=value, reason=reason.strip())


class CostBudget:
    def __init__(
        self,
        limits: BudgetLimits,
        ledger: SpendLedger,
        prices: Mapping[str, tuple[float, float]],
        override: RunOverride | None = None,
    ) -> None:
        self.limits = limits
        self.override = override
        self._ledger = ledger
        self._prices = prices
        self.run_calls = 0
        self.run_usd = 0.0

    def cost(self, model: str, completion: Completion) -> float:
        pin, pout = self._prices[model]
        return (completion.input_tokens * pin + completion.output_tokens * pout) / 1_000_000

    def check_lifetime(self) -> None:
        spent = self._ledger.total_usd()
        if spent >= self.limits.lifetime_max_usd:
            raise BudgetExceededError(
                f"budget exceeded: lifetime spend ${spent:.4f}/${self.limits.lifetime_max_usd}; refusing new work"
            )

    def check(self, state: MatchState) -> None:
        """Raise before a call that would start above any cap."""
        u, lim = state.usage, self.limits
        for limit, spent, cap in (
            ("match_calls", u.calls, lim.match_max_calls),
            ("match_tokens", u.tokens, lim.match_max_tokens),
            ("match_usd", u.usd, lim.match_max_usd),
        ):
            if spent >= cap:
                raise MatchCapError(limit, spent, cap)  # terminal CAPPED, never resumable (ADR 0014)
        breaches = [  # run and lifetime caps stay resumable failures
            (self.run_calls >= lim.run_max_calls, f"run calls {self.run_calls}/{lim.run_max_calls}"),
            (self.run_usd >= lim.run_max_usd, f"run cost ${self.run_usd:.4f}/${lim.run_max_usd}"),
        ]
        for hit, what in breaches:
            if hit:
                raise BudgetExceededError(f"budget exceeded: {what}")
        self.check_lifetime()

    def reserve(self, match_id: str, model: str, request: CompletionRequest) -> str:
        """ledger the worst case (prompt chars as tokens + ``max_tokens`` out) before the call."""
        pin, pout = self._prices[model]
        usd = (len(request.system) + len(request.prompt)) * pin / 1_000_000 + request.max_tokens * pout / 1_000_000
        return self._ledger.reserve(match_id, model, usd)

    def release(self, match_id: str, model: str, reservation: str) -> None:
        """The call failed without a charge: cancel its reservation."""
        self._ledger.record(match_id, model, 0, 0.0, settles=reservation)

    def charge(self, match_id: str, model: str, completion: Completion, reservation: str | None = None) -> float:
        """Count the call against the run caps and the shared ledger; return its cost in USD."""
        usd = self.cost(model, completion)
        self.run_calls += 1
        self.run_usd += usd
        # Every call is ledgered, $0 included, so mispriced or free models stay visible.
        self._ledger.record(match_id, model, completion.total_tokens, usd, settles=reservation)
        return usd
