"""The key limit is a ceiling check, not the figure shown in output (#195)."""

from claude_duel.app.budget import BudgetLimits


def test_key_limit_description_is_a_ceiling() -> None:
    desc = BudgetLimits.model_fields["key_limit_usd"].description or ""
    assert "lifetime_max_usd" in desc
    assert "shown in output" not in desc
