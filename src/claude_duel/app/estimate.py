"""Offline cost preview per preset (#37): an upper bound from the rules, the price table and the budget caps."""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits
from claude_duel.domain.models import Rules

IN_TOKENS, OUT_TOKENS = 2000, 400  # assumed tokens per call (docs/design/design-rules-abstraction.md)


@dataclass(frozen=True)
class Estimate:
    mode: str
    model: str
    calls: int
    tokens: int
    usd: float
    bound: str  # which limit set the token figure: "rules", "wallet" or "match cap"


def estimate(rules: Rules, model: str, limits: BudgetLimits) -> Estimate:
    """Worst-case calls and tokens for one match, priced at ``DEFAULT_PRICES[model]``; no API calls."""
    pin, pout = DEFAULT_PRICES[model]
    out = min(OUT_TOKENS, rules.max_output_tokens, rules.wallet.max_output_tokens or OUT_TOKENS)
    per_call_usd = (IN_TOKENS * pin + out * pout) / 1_000_000
    per_call_tokens = IN_TOKENS + out
    builds = 1 + rules.max_format_retries + (1 if rules.difficulty_gate else 0)
    bonus = rules.early_bonus_attempts
    calls = min(
        rules.rounds * 2 * rules.challenges_per_round * (builds + rules.max_attempts + bonus),
        limits.match_max_calls,
    )
    bounds = [(calls * per_call_tokens, "rules"), (limits.match_max_tokens, "match cap")]
    if rules.wallet.per_agent_tokens is not None:
        bounds.append((2 * rules.wallet.per_agent_tokens, "wallet"))
    tokens, bound = min(bounds)
    usd = min(tokens / per_call_tokens * per_call_usd, limits.match_max_usd)
    if rules.wallet.per_agent_usd is not None and 2 * rules.wallet.per_agent_usd < usd:
        usd, bound = 2 * rules.wallet.per_agent_usd, "wallet"
    return Estimate(rules.mode, model, min(calls, -(-tokens // per_call_tokens)), tokens, usd, bound)


def render(rows: list[Estimate]) -> str:
    lines = [f"{'mode':<11}{'model':<14}{'calls':>6}{'tokens':>9}{'usd':>10}  bound"]
    lines += [f"{e.mode:<11}{e.model:<14}{e.calls:>6}{e.tokens:>9}{e.usd:>10.4f}  {e.bound}" for e in rows]
    return "\n".join(lines)
