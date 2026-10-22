"""The model allowlist is code-defined; config cannot extend it or change prices (spend safety)."""

from pathlib import Path

import pytest

from claude_duel.app.budget import DEFAULT_PRICES, check_model_allowed
from claude_duel.config import load_settings
from claude_duel.domain.errors import ConfigError


def test_config_cannot_add_prices(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('openai_model = "gpt-4o"\n[prices]\n"gpt-4o" = [0.0, 0.0]\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg, {})


def test_unlisted_model_is_refused_from_config_and_env(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="allowlist"):
        load_settings(None, {"DUEL_OPENAI_MODEL": "gpt-4o"})
    cfg = tmp_path / "a.toml"
    cfg.write_text(
        '[[agents]]\nname = "a"\nprovider = "openai"\nmodel = "o1"\n[[agents]]\nname = "b"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_settings(cfg, {})


def test_every_allowed_model_passes() -> None:
    for model in DEFAULT_PRICES:
        check_model_allowed(model)
    with pytest.raises(ConfigError):
        check_model_allowed("gpt-5")
