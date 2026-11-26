"""Fixed boards per pairing and the calibrated cost basis: money never shortens a match, and a plan whose fixed
schedule cannot fit the caps at measured token use is refused at $0."""

from __future__ import annotations

from typing import Any

import pytest

from claude_duel.app import calibration
from claude_duel.app import ladder_config as lc
from claude_duel.app.ladder import match_usd, project
from claude_duel.domain import duplicate as dp
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec, Rules, SandboxLimits


def _plan(**over: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "run_date": "2026-11-20",
        "sides": {"a": ["claude-fable-5-2", "claude-haiku-5-5"], "b": ["gpt-6.2-astra", "gpt-6-luna"]},
        "match": {"boards": 12},
        "budget": {"match_max_usd": 25.0, "ladder_max_usd": 95.0, "basis": "calibrated"},
        "caps": {"build": 24000, "allocate": 2000, "attack": 8000},
    }
    return plan | over


def test_fixed_boards_play_exactly_n_with_no_early_stop() -> None:
    rule = lc.parse(_plan(), allow_premium=True).duplicate_rule()
    assert (rule.boards_min, rule.boards_max, rule.stop) == (12, 12, "none")
    lopsided = [10] * 11
    assert not dp.stop_now(rule, 11, lopsided)  # a runaway lead still plays every board
    assert dp.stop_now(rule, 12, [*lopsided, 10])


def test_fixed_boards_and_a_range_are_refused_together() -> None:
    with pytest.raises(ConfigError, match="not both"):
        lc.parse(_plan(match={"boards": 12, "boards_max": 20}), allow_premium=True)


def _state(boards: int, *, capped: bool, rule: dp.DuplicateRule) -> dp.DuplicateState:
    agents = (AgentSpec(name="p1", model="fake-1"), AgentSpec(name="p2", model="fake-1"))
    state = dp.DuplicateState(
        duplicate_id="d", seed="s", rule=rule, agents=agents, rules=Rules(), limits=SandboxLimits(), capped=capped
    )
    for n in range(1, boards + 1):
        state.boards.append(dp.Board(board_no=n, seed=f"{n}", halves=(f"{n}a", f"{n}b"), raw=10, diff=10))
    return state


@pytest.mark.parametrize("stop", ["none", "margin"])
def test_a_money_stop_before_the_scheduled_boards_is_capped_never_a_verdict(stop: str) -> None:
    rule = dp.DuplicateRule(boards_min=12, boards_max=12, stop=stop)
    assert _state(7, capped=True, rule=rule).result() == "capped"  # 7 straight wins, but price ended it
    assert _state(12, capped=False, rule=rule).result() == "first"


def test_calibrated_usage_uses_measured_rows_and_named_proxies() -> None:
    assert calibration.usage("claude-fable-5-2").source == "claude-fable-5-1"
    assert calibration.usage("gpt-6-luna").source == "gpt-6-luna"
    with pytest.raises(ConfigError, match="no calibrated token use"):
        calibration.usage("gpt-5-mini")


def test_calibrated_price_follows_each_models_measured_output() -> None:
    cfg = lc.parse(_plan(), allow_premium=True)
    rules = cfg.rules().model_copy(update={"reasoning_effort": None})  # each row at the effort it was logged at
    # fable writes ~2.6x astra's mean output at the same list price ratio: it must price dearer, not equal
    fable = calibration.calibrated_agent_usd(rules, "claude-fable-5-2")
    assert fable > 2 * calibration.calibrated_agent_usd(rules, "gpt-6.2-astra")
    assert match_usd(cfg, "claude-haiku-5-5", "gpt-6-luna", basis="calibrated") < 1.0


def test_dry_run_refuses_a_fixed_schedule_that_cannot_fit_the_caps() -> None:
    proj = project(lc.parse(_plan(), allow_premium=True))
    assert any(r.startswith("claude-fable-5-2 vs gpt-6.2-astra: calibrated case") for r in proj.refusals)
    cheap = _plan(
        sides={"a": ["claude-haiku-5-5"], "b": ["gpt-6-luna"]},
        budget={"match_max_usd": 4.5, "ladder_max_usd": 4.5, "basis": "calibrated"},
    )
    assert project(lc.parse(cheap)).refusals == []


def test_calibrated_basis_refuses_an_unmeasured_model() -> None:
    plan = _plan(sides={"a": ["claude-haiku-5-5"], "b": ["gpt-5-mini"]})
    with pytest.raises(ConfigError, match="no calibrated token use"):
        project(lc.parse(plan))


def test_high_effort_scales_output_from_each_rows_logged_effort() -> None:
    # luna was logged at low, astra at medium, Claude at its API default (high): high doubles astra, quadruples luna
    assert calibration.effort_scale("gpt-6-luna", "high") == 4.0
    assert calibration.effort_scale("gpt-6-astra", "high") == 2.0
    assert calibration.effort_scale("claude-haiku-5-5", "high") == 1.0
    assert calibration.effort_scale("gpt-6-astra", None) == 1.0
    high = lc.parse(_plan(caps={**_plan()["caps"], "effort": "high"}), allow_premium=True)
    low = lc.parse(_plan(caps={**_plan()["caps"], "effort": "low"}), allow_premium=True)
    assert match_usd(high, "claude-haiku-5-5", "gpt-6-luna", basis="calibrated") > match_usd(
        low, "claude-haiku-5-5", "gpt-6-luna", basis="calibrated"
    )


def test_pair_caps_raise_only_the_named_pairings() -> None:
    pricey = "claude-fable-5-2 vs gpt-6.2-astra"
    budget = {"match_max_usd": 5.0, "ladder_max_usd": 900.0, "basis": "calibrated", "pair_max_usd": {pricey: 300.0}}
    cfg = lc.parse(_plan(budget=budget), allow_premium=True)
    assert cfg.match_cap("claude-fable-5-2", "gpt-6.2-astra") == 300.0
    assert cfg.duplicate_rule("claude-fable-5-2", "gpt-6.2-astra").match_max_usd == 300.0
    assert cfg.match_cap("claude-haiku-5-5", "gpt-6-luna") == 5.0
    assert cfg.top_match_cap() == 300.0
    refused = project(cfg).refusals
    assert not any(r.startswith(pricey) for r in refused)
    assert any(r.startswith("claude-fable-5-2 vs gpt-6-luna") for r in refused)


@pytest.mark.parametrize(
    ("pairs", "why"),
    [
        ({"claude-haiku-5-5 vs claude-fable-5-2": 30.0}, "not a pairing"),
        ({"claude-haiku-5-5 vs gpt-6-luna": 0.0}, "> 0"),
    ],
)
def test_pair_caps_name_real_pairings_and_stay_under_the_ladder_cap(pairs: dict[str, float], why: str) -> None:
    budget = {"match_max_usd": 25.0, "ladder_max_usd": 95.0, "basis": "calibrated", "pair_max_usd": pairs}
    with pytest.raises(ConfigError, match=why):
        lc.parse(_plan(budget=budget), allow_premium=True)
