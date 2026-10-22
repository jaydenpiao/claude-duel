"""The launch config pins what docs/runs/launch-prereg.md says (#148), and stays symmetric."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from claude_duel.app.budget import DEFAULT_PRICES, PREMIUM, BudgetLimits
from claude_duel.config import load_settings

ROOT = Path(__file__).parent.parent.parent
CONFIG = ROOT / "docs" / "runs" / "launch.toml"


def test_launch_config_matches_prereg() -> None:
    s = load_settings(CONFIG, env={})
    r = s.rules
    assert (r.mode, r.rounds, r.challenges_per_round, r.max_attempts) == ("checked", 3, 1, 3)
    assert (r.max_output_tokens, r.reasoning_effort, r.variety) == (8000, "low", True)
    assert r.framing.kind == "neutral"
    assert [a.name for a in s.agents] == ["a", "b"]  # neutral labels, never vendor names
    assert all(a.handicap == s.agents[0].handicap for a in s.agents)
    doc = (ROOT / "docs" / "runs" / "launch-prereg.md").read_text(encoding="utf-8")
    assert "`max_output_tokens = 8000`" in doc
    assert '`reasoning_effort = "low"`' in doc


def _typical_seed_usd(arm: dict[str, Any]) -> float:
    """Typical cost of one seed (2 matches), per the prereg cost table: 6 calls per agent, 1,500 in / 2,000 out."""
    per_match = sum(
        6 * (1500 * DEFAULT_PRICES[m][0] + 2000 * DEFAULT_PRICES[m][1]) / 1e6 for m in (arm["anthropic"], arm["openai"])
    )
    return 2 * per_match


def test_launch_arms_match_prereg_and_fit_the_ledger() -> None:
    arms = tomllib.loads((ROOT / "docs" / "runs" / "launch-arms.toml").read_text(encoding="utf-8"))["arm"]
    assert [a["name"] for a in arms] == ["smoke", "premium", "flagship"]  # pre-registered execution order
    assert sum(a["run_max_usd"] for a in arms) == 33.0
    assert sum(a["run_max_usd"] for a in arms) < BudgetLimits().lifetime_max_usd
    doc = (ROOT / "docs" / "runs" / "launch-prereg.md").read_text(encoding="utf-8")
    for order, a in enumerate(arms, 1):
        assert a["anthropic"] in DEFAULT_PRICES
        assert a["openai"] in DEFAULT_PRICES
        assert a["match_max_usd"] <= a["run_max_usd"]
        row = f"| {order} | {a['name']}"
        line = next(ln for ln in doc.splitlines() if ln.startswith(row))
        cells = [c.strip() for c in line.split("|")]
        assert cells[3:5] == [a["anthropic"], a["openai"]]
        assert [float(cells[6]), float(cells[7]), float(cells[8])] == [a["n"], a["run_max_usd"], a["match_max_usd"]]
    for a in arms[1:]:
        assert {a["anthropic"], a["openai"]} <= PREMIUM  # needs allow_premium, set per run, never by default
        assert a["n"] * _typical_seed_usd(a) <= a["run_max_usd"]  # a typical arm finishes all N within its cap
    for a in arms[1:]:
        assert a["n"] * _typical_seed_usd(a) <= a["run_max_usd"] / 1.15  # owner's 15% headroom
        assert (a["n"] + 1) * _typical_seed_usd(a) > a["run_max_usd"] / 1.15  # and N is the largest that fits
    premium = arms[1]
    assert DEFAULT_PRICES[premium["anthropic"]] != DEFAULT_PRICES[premium["openai"]]
    assert "not price-matched" in doc
    assert "$/win" in doc
    assert "Holm" in doc
    assert "**void seed**" in doc
    assert "never re-run" in doc
    assert "**more than 20%**" in doc
    assert "**cap-limited**" in doc
    assert "breaching agent" in doc
