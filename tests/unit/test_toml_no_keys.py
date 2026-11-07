"""API keys load from env only, never from a config file."""

from pathlib import Path

import pytest

from claude_duel.config import load_settings
from claude_duel.domain.errors import ConfigError


@pytest.mark.parametrize("body", ['openai_api_key = "sk-x"\n', '[api_keys]\nopenai = "sk-x"\n'])
def test_toml_key_refused(tmp_path: Path, body: str) -> None:
    cfg = tmp_path / "duel.toml"
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="env"):
        load_settings(cfg, env={})


def test_env_key_still_loads(tmp_path: Path) -> None:
    cfg = tmp_path / "duel.toml"
    cfg.write_text("", encoding="utf-8")
    assert "openai" in load_settings(cfg, env={"OPENAI_API_KEY": "sk-test"}).api_keys
