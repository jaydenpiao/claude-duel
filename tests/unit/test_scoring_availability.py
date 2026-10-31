from pathlib import Path

import pytest

from claude_duel.config import load_settings
from claude_duel.domain.errors import ConfigError


def _cfg(tmp_path: Path, kind: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(f'[rules.scoring]\nkind = "{kind}"\n')
    return p


def test_ledger_scoring_is_rejected_until_192(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"scoring kind 'ledger' is not available yet.*#192"):
        load_settings(_cfg(tmp_path, "ledger"), env={})


@pytest.mark.parametrize("kind", ["classic", "efficiency", "timed"])
def test_other_scoring_kinds_still_load(tmp_path: Path, kind: str) -> None:
    assert load_settings(_cfg(tmp_path, kind), env={}).rules.scoring.kind == kind
