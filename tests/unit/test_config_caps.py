from pathlib import Path

import pytest

from claude_duel.app.budget import BudgetLimits
from claude_duel.config import load_settings

CONFIGS = sorted((Path(__file__).resolve().parents[2] / "configs").glob("*.toml"))


def test_configs_found() -> None:
    assert {p.name for p in CONFIGS} >= {"dev.toml", "staging.toml", "prod.toml"}


EVAL = "eval.toml"  # the final eval profile: its lifetime cap is raised on purpose (jayden, 2026-11-26)


@pytest.mark.parametrize("path", [p for p in CONFIGS if p.name != EVAL], ids=lambda p: p.name)
def test_every_config_matches_default_lifetime_cap(path: Path) -> None:
    assert load_settings(path, env={}).budget.lifetime_max_usd == BudgetLimits().lifetime_max_usd


def test_eval_profile_pins_its_raised_cap() -> None:
    budget = load_settings(CONFIGS[0].parent / EVAL, env={}).budget
    assert budget.lifetime_max_usd == 1450.0 <= budget.key_limit_usd
