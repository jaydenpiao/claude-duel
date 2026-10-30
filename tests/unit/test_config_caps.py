from pathlib import Path

import pytest

from claude_duel.config import load_settings

CONFIGS = sorted((Path(__file__).resolve().parents[2] / "configs").glob("*.toml"))


def test_configs_found() -> None:
    assert {p.name for p in CONFIGS} >= {"dev.toml", "staging.toml", "prod.toml"}


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_every_config_caps_lifetime_spend_at_33(path: Path) -> None:
    assert load_settings(path, env={}).budget.lifetime_max_usd == 33.0
