"""Per-agent wallets (#35): a game rule folded from ``call_charged``, checked before the safety caps.

``CostBudget`` stays the safety floor; a wallet only tightens it. A call that starts under the wallet is
allowed, so an agent can overshoot by at most one call (same convention as ``CostBudget.check``).
"""

from claude_duel.domain.errors import WalletEmptyError
from claude_duel.domain.models import MatchState, Usage


def empty(state: MatchState, agent: str) -> str | None:
    """Why ``agent``'s wallet is spent, or None while it can still pay for a call."""
    wallet = state.rules.wallet
    used = state.usage_by_agent.get(agent, Usage())
    if wallet.per_agent_tokens is not None and used.tokens >= wallet.per_agent_tokens:
        return f"wallet empty: {agent} used {used.tokens}/{wallet.per_agent_tokens} tokens"
    if wallet.per_agent_usd is not None and used.usd >= wallet.per_agent_usd:
        return f"wallet empty: {agent} spent ${used.usd:.4f}/${wallet.per_agent_usd}"
    return None


def check(state: MatchState, agent: str) -> None:
    if (reason := empty(state, agent)) is not None:
        raise WalletEmptyError(reason)
