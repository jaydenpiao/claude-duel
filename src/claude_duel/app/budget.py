"""Cost circuit breaker.

Caps: per match (persisted in state), per run (this process only), and lifetime (the shared
ledger, re-read before every call so concurrent processes see each other's spend). Concurrent
processes can each overshoot the lifetime cap by at most one in-flight call.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from claude_duel.domain.errors import BudgetExceededError, ConfigError
from claude_duel.domain.models import MatchState
from claude_duel.ports import Completion, SpendLedger

# Default USD per 1M tokens (input, output). Low/medium-tier models only; override via [prices] in config.
FAKE_MODEL = "fake-1"
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    FAKE_MODEL: (0.0, 0.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1-mini": (0.40, 1.60),
}


def check_model_allowed(model: str, prices: Mapping[str, tuple[float, float]]) -> None:
    if model not in prices:
        raise ConfigError(f"model {model!r} is not in the price table / allowlist: {sorted(prices)}")


class BudgetLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    match_max_calls: int = Field(default=80, ge=1)
    match_max_tokens: int = Field(default=150_000, ge=1)
    match_max_usd: float = Field(default=0.25, gt=0)
    run_max_calls: int = Field(default=200, ge=1)
    run_max_usd: float = Field(default=0.50, gt=0)
    lifetime_max_usd: float = Field(default=40.0, gt=0)


class CostBudget:
    def __init__(self, limits: BudgetLimits, ledger: SpendLedger, prices: Mapping[str, tuple[float, float]]) -> None:
        self.limits = limits
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
        breaches = [
            (u.calls >= lim.match_max_calls, f"match calls {u.calls}/{lim.match_max_calls}"),
            (u.tokens >= lim.match_max_tokens, f"match tokens {u.tokens}/{lim.match_max_tokens}"),
            (u.usd >= lim.match_max_usd, f"match cost ${u.usd:.4f}/${lim.match_max_usd}"),
            (self.run_calls >= lim.run_max_calls, f"run calls {self.run_calls}/{lim.run_max_calls}"),
            (self.run_usd >= lim.run_max_usd, f"run cost ${self.run_usd:.4f}/${lim.run_max_usd}"),
        ]
        for hit, what in breaches:
            if hit:
                raise BudgetExceededError(f"budget exceeded: {what}")
        self.check_lifetime()

    def charge(self, match_id: str, model: str, completion: Completion) -> float:
        """Count the call against the run caps and the shared ledger; return its cost in USD."""
        usd = self.cost(model, completion)
        self.run_calls += 1
        self.run_usd += usd
        # Every call is ledgered, $0 included, so mispriced or free models stay visible.
        self._ledger.record(match_id, model, completion.total_tokens, usd)
        return usd
