"""Per-agent allowances (#35 wallets, #36 token clock): one fold over ``call_charged``, checked before the caps.

``CostBudget`` stays the safety floor; a wallet only tightens it. A call that starts under the wallet is
allowed, so an agent can overshoot by at most one call (same convention as ``CostBudget.check``).
"""

from claude_duel.domain.errors import WalletEmptyError
from claude_duel.domain.models import MatchState, Rules, Usage


def empty(state: MatchState, agent: str) -> str | None:
    """Why ``agent``'s wallet is spent, or None while it can still pay for a call."""
    wallet = state.rules.wallet
    used = state.usage_by_agent.get(agent, Usage())
    if wallet.per_agent_tokens is not None and used.tokens >= wallet.per_agent_tokens:
        return f"wallet empty: {agent} used {used.tokens}/{wallet.per_agent_tokens} tokens"
    if wallet.per_agent_usd is not None and used.usd >= wallet.per_agent_usd:
        return f"wallet empty: {agent} spent ${used.usd:.4f}/${wallet.per_agent_usd}"
    return clock_fallen(state, agent)


def clock_left(state: MatchState, agent: str) -> int | None:
    """Clock tokens ``agent`` has left (bank + increments - charged), or None without a clock."""
    clock = state.rules.clock
    if clock.bank_tokens is None:
        return None
    used = state.usage_by_agent.get(agent, Usage())
    bank = int(clock.bank_tokens * state.handicap(agent).bank_mult)
    return bank + clock.increment_tokens * used.calls - used.clock_tokens


def clock_fallen(state: MatchState, agent: str) -> str | None:
    left = clock_left(state, agent)
    if left is None or left > 0:
        return None
    return f"clock: {agent}'s flag fell ({left} clock tokens left)"


def check(state: MatchState, agent: str) -> None:
    if (reason := empty(state, agent)) is not None:
        raise WalletEmptyError(reason)


def wallets_exceed(rules: Rules, agents: int, match_max_usd: float) -> str | None:
    """Warning when the per-agent USD wallets sum above ``match_max_usd``: the match cap then ends the match before
    every wallet can be spent, so later agents get less than their wallet."""
    per = rules.wallet.per_agent_usd
    if per is None or per * agents <= match_max_usd:
        return None
    total = per * agents
    return f"wallets {agents} x ${per} = ${total:.4f} exceed match_max_usd ${match_max_usd}; the match cap binds first"
