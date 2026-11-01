from pathlib import Path

import pytest

from claude_duel.config import load_settings


def _cfg(tmp_path: Path, kind: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(f'[rules.scoring]\nkind = "{kind}"\n')
    return p


@pytest.mark.parametrize("kind", ["classic", "efficiency", "timed", "ledger"])
def test_every_scoring_kind_loads(tmp_path: Path, kind: str) -> None:
    """Ledger stakes are wired into matches here, so ledger scoring loads like the others."""
    assert load_settings(_cfg(tmp_path, kind), env={}).rules.scoring.kind == kind
