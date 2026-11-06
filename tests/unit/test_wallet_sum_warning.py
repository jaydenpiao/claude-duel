"""A match whose USD wallets sum above match_max_usd warns at creation (the match cap would bind first)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_duel.app.allowance import wallets_exceed
from claude_duel.app.budget import BudgetLimits
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain.models import Rules, WalletRule
from claude_duel.observability import InMemoryMetrics


def test_wallets_exceed() -> None:
    rules = Rules(wallet=WalletRule(per_agent_usd=0.6))
    assert "exceed match_max_usd $1.0" in (wallets_exceed(rules, 2, 1.0) or "")
    assert wallets_exceed(rules, 2, 1.2) is None
    assert wallets_exceed(Rules(), 2, 0.01) is None


@pytest.mark.parametrize(("per", "warned"), [(0.75, True), (0.25, False)])
def test_match_creation_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture, per: float, warned: bool) -> None:
    settings = Settings(
        store_dir=tmp_path,
        sandbox="subprocess",
        rules=Rules(rounds=1, challenges_per_round=1, wallet=WalletRule(per_agent_usd=per)),
        budget=BudgetLimits(match_max_usd=1.0),
    )
    with caplog.at_level(logging.WARNING):
        play(settings, "m1", InMemoryMetrics())
        play(settings, "m1", InMemoryMetrics())  # resume: no second warning
    assert sum("exceed match_max_usd" in r.getMessage() for r in caplog.records) == int(warned)
