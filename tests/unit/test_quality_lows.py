"""Cross-module consistency checks from the 2026-10-30 PM quality review (L24, L26)."""

from __future__ import annotations

import pytest

from claude_duel.adapters.store.postgres import PUBLIC_VIEWS
from claude_duel.app import container
from claude_duel.app.export import FIELDS
from claude_duel.config import load_settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.memory import TECHNIQUES
from claude_duel.domain.prophecy import TEMPLATES
from claude_duel.domain.stakes import LedgerRule


def test_every_uses_construct_template_names_a_known_technique() -> None:
    tags = [a.tag for atoms in TEMPLATES.values() for a in atoms if a.kind == "uses_construct"]
    assert tags
    assert set(tags) <= set(TECHNIQUES)


def test_all_in_is_half_the_attack_budget() -> None:
    (atom,) = TEMPLATES["all_in"]
    assert atom.n == LedgerRule().attack_tokens // 2


def test_public_views_match_export_fields() -> None:
    assert tuple(FIELDS) == PUBLIC_VIEWS


def test_results_db_without_psycopg_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(container.importlib, "import_module", missing)
    with pytest.raises(ConfigError, match=r"claude-duel\[db\]"):
        container.migrate_results(load_settings(None, env={"MIGRATE_DATABASE_URL": "postgresql://x@localhost/x"}))
