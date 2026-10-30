import pytest

from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, check_model_allowed
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError


def test_defaults_use_a_live_model_and_the_new_caps() -> None:
    s = Settings()
    assert s.openai_model == s.gate_model == "gpt-5-nano"
    assert s.openai_model in DEFAULT_PRICES
    lim = BudgetLimits()
    assert (lim.lifetime_max_usd, lim.match_max_usd) == (33.0, 0.25)


def test_retired_gpt_4_1_nano_is_not_allowed() -> None:
    assert "gpt-4.1-nano" not in DEFAULT_PRICES
    with pytest.raises(ConfigError):
        check_model_allowed("gpt-4.1-nano")
