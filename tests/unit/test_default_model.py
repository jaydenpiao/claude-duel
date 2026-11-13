import pytest

from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, check_model_allowed
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError


def test_defaults_use_a_live_model_and_the_new_caps() -> None:
    s = Settings()
    assert s.default_model("openai") == s.gate_model == "gpt-6-luna"
    assert DEFAULT_PRICES["gpt-6-luna"] == (0.10, 0.50)
    assert s.default_model("openai") in DEFAULT_PRICES
    lim = BudgetLimits()
    assert (lim.lifetime_max_usd, lim.match_max_usd) == (33.0, 0.25)


def test_retired_gpt_4_1_nano_is_not_allowed() -> None:
    assert "gpt-4.1-nano" not in DEFAULT_PRICES
    with pytest.raises(ConfigError):
        check_model_allowed("gpt-4.1-nano")


def test_chat_models_are_all_allowed() -> None:
    from claude_duel.ports import CHAT_MODELS  # noqa: PLC0415

    assert CHAT_MODELS.issubset(DEFAULT_PRICES)
