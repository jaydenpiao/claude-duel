"""docs/design carries the game designer's approved decisions (memory budget, stakes override)."""

from pathlib import Path

DESIGN = Path(__file__).resolve().parents[2] / "docs" / "design"


def test_memory_spec_has_token_budget_and_reveal_rule() -> None:
    text = (DESIGN / "design-memory-strategy.md").read_text(encoding="utf-8")
    assert "MemoryRule.max_tokens" in text
    assert "MemoryRule.source_tokens" in text
    assert "round_scored" in text


def test_stakes_spec_has_scoped_run_override() -> None:
    text = (DESIGN / "design-stakes-modes.md").read_text(encoding="utf-8")
    assert "RUN_OVERRIDE_CEILING_USD" in text
    assert "--override-reason" in text
    assert "budget_overridden" in text
