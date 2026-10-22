"""Every DUEL_* knob and allowed model is documented (#15), and the new overrides apply."""

from pathlib import Path

from claude_duel.app.budget import DEFAULT_PRICES, FAKE_MODEL
from claude_duel.config import ENV_VARS, load_settings

README = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")


def test_every_env_var_is_in_readme() -> None:
    assert [v for v in ENV_VARS if f"`{v}`" not in README] == []


def test_every_allowed_model_is_in_readme() -> None:
    assert [m for m in DEFAULT_PRICES if m != FAKE_MODEL and f"`{m}`" not in README] == []


def test_new_overrides_apply() -> None:
    env = {"DUEL_PROVIDER_MAX_RETRIES": "2", "DUEL_RUN_MAX_CALLS": "7", "DUEL_LIFETIME_MAX_USD": "5"}
    s = load_settings(None, env)
    assert (s.provider_max_retries, s.budget.run_max_calls, s.budget.lifetime_max_usd) == (2, 7, 5.0)
