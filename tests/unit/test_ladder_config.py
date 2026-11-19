"""Ladder plan validator (design-no-fluke §4.1): every rule fails with its own named error, at $0."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_duel.app import ladder_config as lc
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import Rules

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
PLANS = ("a", "b", "c", "fake")


def _plan(**over: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "run_date": "2026-11-20",
        "sides": {"a": ["claude-opus-5-5", "claude-haiku-5-5"], "b": ["gpt-5-mini", "gpt-5-nano"]},
        "budget": {"match_max_usd": 5.0, "ladder_max_usd": 10.0},
        "caps": {"build": 2000, "allocate": 500, "attack": 1000},
    }
    return plan | over


@pytest.mark.parametrize("name", PLANS)
def test_examples_validate(name: str) -> None:
    cfg = lc.load(EXAMPLES / f"ladder-{name}.toml", {"run_date": "2026-11-20"})
    assert cfg.run_date.isoformat() == "2026-11-20"


@pytest.mark.parametrize(
    ("plan", "overrides", "error"),
    [
        (_plan(sides={"a": ["claude-nope"], "b": ["gpt-5-mini"]}), {}, "unknown model 'claude-nope'"),
        (_plan(sides={"a": ["claude-opus-5-5", "gpt-5-mini"], "b": ["gpt-5-nano"]}), {}, "but side a is anthropic"),
        (_plan(), {"start_tier.b": 2}, r"start_tier.b=2 is out of range"),
        (_plan(), {"start_tier.a": -1}, r"start_tier.a=-1 is out of range"),
        (_plan(match={"boards_min": 5, "boards_max": 4}), {}, "boards_min must be >= 1 and <= boards_max"),
        (_plan(match={"boards_min": 0, "boards_max": 4}), {}, "boards_min must be >= 1"),
        (_plan(match={"boards_min": 1, "boards_max": 65}), {}, "boards_max <= 64"),
        (_plan(), {"caps.attack": 0}, "attack must be > 0"),
        (_plan(), {"budget.match_max_usd": -1.0}, "match_max_usd must be > 0"),
        (_plan(), {"budget.match_max_usd": 11.0}, "match_max_usd must be <= ladder_max_usd"),
        ({k: v for k, v in _plan().items() if k != "run_date"}, {}, "run_date is required"),
        (_plan(run_date="20/11/2026"), {}, "run_date must be an ISO-8601 date"),
        (_plan(sides={"a": ["fake-1"], "b": ["gpt-5-mini"]}), {}, "fake-1 cannot be mixed with real models"),
        (_plan(caps={"a": {"build": 2000}, "b": {"build": 4000}}), {}, "caps must be the same for both sides"),
        (_plan(caps={"build": 10, "allocate": 500, "attack": 1000}), {}, "bad caps"),
        (_plan(colour="red"), {}, "bad ladder config: colour"),
    ],
)
def test_validator_names_each_failure(plan: dict[str, Any], overrides: dict[str, Any], error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        lc.parse(plan, overrides)


def test_nano_cutoff_follows_run_date_not_the_clock() -> None:
    assert lc.parse(_plan(run_date="2026-12-10")).run_date.isoformat() == "2026-12-10"
    with pytest.raises(
        ConfigError, match=r"gpt-5-nano is retired from 2026-12-11 \(run_date=2026-12-11\); use gpt-5-mini"
    ):
        lc.parse(_plan(run_date="2026-12-11"))
    with pytest.raises(ConfigError, match="retired"):
        lc.load(EXAMPLES / "ladder-a.toml", {"run_date": "2027-01-01"})


def test_flags_override_the_file() -> None:
    cfg = lc.load(
        EXAMPLES / "ladder-a.toml",
        {"sides.b": ["gpt-5-mini"], "start_tier.a": 2, "match.boards_max": 6, "caps.max_attempts": 2},
    )
    assert cfg.sides.b == ("gpt-5-mini",)
    assert (cfg.start_tier.a, cfg.match.boards_max, cfg.caps.max_attempts) == (2, 6, 2)


def test_plan_maps_to_ladder_rules_agents_and_a_stable_digest() -> None:
    cfg = lc.parse(_plan())
    rules = cfg.rules()
    assert rules.mode == "ladder"
    assert rules.output_cap(purpose="build") == 2000
    assert rules.output_cap(purpose="allocate") == 500
    assert rules.output_cap(purpose="attack") == 1000
    assert rules.output_cap(own=700, purpose="build") == 700  # an agent's own cap still wins
    assert cfg.duplicate_rule().match_max_usd == 5.0
    assert cfg.digest() == lc.parse(_plan()).digest() != lc.parse(_plan(run_date="2026-11-21")).digest()
    a, b = lc.agents("claude-opus-5-5", "gpt-5.6-sol")
    assert (a.name, a.provider, b.name, b.provider) == ("claude-opus-5-5", "anthropic", "gpt-5-6-sol", "openai")
    assert [x.name for x in lc.agents("fake-1", "fake-1")] == ["fake-1-a", "fake-1-b"]


def test_call_caps_stay_out_of_logs_unless_set() -> None:
    assert "call_caps" not in Rules().dump()
    assert lc.parse(_plan()).rules().dump()["call_caps"] == {"build": 2000, "allocate": 500, "attack": 1000}
