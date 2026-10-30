"""`duel estimate` (#37): offline, bounded by the rules, the wallet and the budget caps."""

from __future__ import annotations

import pytest

from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits
from claude_duel.app.estimate import IN_TOKENS, OUT_TOKENS, estimate
from claude_duel.cli import main
from claude_duel.domain.models import Rules, WalletRule
from claude_duel.domain.modes import MODES, preset

MODEL = "gpt-4o-mini"


def _blended(model: str) -> float:
    pin, pout = DEFAULT_PRICES[model]
    return (IN_TOKENS * pin + OUT_TOKENS * pout) / (IN_TOKENS + OUT_TOKENS) / 1_000_000


@pytest.mark.parametrize("name", sorted(MODES))
def test_every_preset_stays_under_the_match_caps(name: str) -> None:
    lim = BudgetLimits()
    e = estimate(preset(name), MODEL, lim)
    assert 0 < e.calls <= lim.match_max_calls
    assert 0 < e.tokens <= lim.match_max_tokens
    assert 0 < e.usd <= lim.match_max_usd


def test_capped_is_at_most_cap_times_price() -> None:
    rules = preset("capped")
    cap = rules.wallet.per_agent_tokens
    assert cap is not None
    e = estimate(rules, MODEL, BudgetLimits())
    assert e.bound == "wallet"
    assert e.usd <= 2 * cap * _blended(MODEL) + 1e-12


def test_usd_wallet_and_small_rules_bind() -> None:
    e = estimate(Rules(wallet=WalletRule(per_agent_usd=0.001)), MODEL, BudgetLimits())
    assert (e.usd, e.bound) == (0.002, "wallet")
    quick = estimate(preset("quick"), MODEL, BudgetLimits())
    assert quick.bound == "rules"
    assert estimate(preset("quick"), "fake-1", BudgetLimits()).usd == 0
    short = estimate(Rules(mode="quick", rounds=1, wallet=WalletRule(max_output_tokens=100)), MODEL, BudgetLimits())
    assert short.usd < estimate(Rules(mode="quick", rounds=1), MODEL, BudgetLimits()).usd


def test_cli_prints_a_row_per_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["estimate"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 + len(MODES)
    assert main(["estimate", "--mode", "capped", "--model", "gpt-5-nano"]) == 0
    assert "wallet" in capsys.readouterr().out
    assert main(["estimate", "--mode", "nope"]) == 2
    assert "unknown mode" in capsys.readouterr().err
    assert main(["estimate", "--model", "gpt-9"]) == 2
