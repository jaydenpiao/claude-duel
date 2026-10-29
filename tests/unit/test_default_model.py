from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits
from claude_duel.config import Settings


def test_defaults_use_a_live_model_and_the_new_caps() -> None:
    s = Settings()
    assert s.openai_model == s.gate_model == "gpt-5-nano"  # gpt-4.1-nano was shut down 2026-10-23
    assert s.openai_model in DEFAULT_PRICES
    lim = BudgetLimits()
    assert (lim.lifetime_max_usd, lim.match_max_usd) == (33.0, 0.25)
