"""Ladder steps (pure fold) and the $0 worst-case dry run that refuses an over-cap plan."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_duel.app import ladder_config as lc
from claude_duel.app.ladder import match_usd, project
from claude_duel.domain import ladder as ld
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import Event


def _load(*a: Any, **k: Any) -> lc.LadderConfig:  # premium plans validate; the premium gate is tested apart
    return lc.load(*a, allow_premium=True, **k)


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _events(*steps: tuple[str, str, str]) -> list[Event]:
    created = {"config": {}, "digest": "d", "a": ["a0", "a1"], "b": ["b0", "b1", "b2"], "start": [0, 0]}
    out = [Event(v=1, seq=0, type=ld.LADDER_CREATED, trace_id="L", data=created)]
    for n, (a, b, result) in enumerate(steps, 1):
        data = {"step_no": n, "a": a, "b": b, "duplicate_id": f"L-s{n}", "result": result, "usd": 0.0}
        out.append(Event(v=1, seq=n, type=ld.STEP_FINISHED, trace_id="L", data=data))
    return out


def test_loser_steps_down_and_no_winner_moves_both() -> None:
    assert ld.advance((0, 0), "first") == (0, 1)
    assert ld.advance((0, 0), "second") == (1, 0)
    assert ld.advance((0, 0), "inconclusive") == (1, 1)
    assert ld.advance((0, 0), "void") == (1, 1)
    state = ld.fold("L", _events(("a0", "b0", "first"), ("a0", "b1", "second")))
    assert state.tiers() == (1, 1)
    assert state.next_pair() == ("a1", "b1")
    assert not state.done()
    assert ld.fold("L", _events(("a0", "b0", "first"), ("a0", "b1", "second"), ("a1", "b1", "void"))).done()


def test_fold_rejects_a_step_off_the_ladder() -> None:
    with pytest.raises(EventLogCorruptError, match="does not follow"):
        ld.fold("L", _events(("a1", "b0", "first")))
    with pytest.raises(EventLogCorruptError, match="unexpected"):
        ld.fold("L", _events(("a0", "b0", "inconclusive"), ("a1", "b1", "inconclusive"), ("a1", "b2", "first")))


def test_dry_run_refuses_over_the_ladder_cap_and_passes_above_it() -> None:
    plan = EXAMPLES / "ladder-a.toml"
    refused = project(_load(plan, {"budget.ladder_max_usd": 30.0, "budget.match_max_usd": 30.0}))
    assert any("ladder worst case" in r for r in refused.refusals)
    worst = refused.total_usd
    assert worst >= 57  # at least the design's §4 worst-case figure
    biggest = max(r.worst_usd for r in refused.rows)
    fits = project(_load(plan, {"budget.ladder_max_usd": worst + 1, "budget.match_max_usd": biggest + 1}))
    assert fits.refusals == []
    assert fits.total_usd == pytest.approx(worst)


def test_one_dear_match_is_refused_even_when_the_total_fits() -> None:
    cfg = _load(EXAMPLES / "ladder-c.toml", {"budget.ladder_max_usd": 10_000.0, "budget.match_max_usd": 100.0})
    refusals = project(cfg).refusals
    assert refusals
    assert all("match_max_usd" in r for r in refusals)
    assert "claude-fable-5-1 vs gpt-6-astra" in refusals[0]


@pytest.mark.parametrize(
    ("name", "total", "rows"),
    [("a", 1743.21, 9), ("b", 606.24, 7), ("c", 770.08, 9), ("fake", 0.0, 3)],
)
def test_projection_golden(name: str, total: float, rows: int) -> None:
    proj = project(_load(EXAMPLES / f"ladder-{name}.toml"))
    assert round(proj.total_usd, 2) == total
    assert len(proj.rows) == rows
    assert all(r.typical_usd <= r.worst_usd for r in proj.rows)
    assert proj.typical_usd() <= proj.total_usd


def test_match_cost_scales_with_boards_and_replays() -> None:
    cfg = _load(EXAMPLES / "ladder-b.toml")
    half = _load(EXAMPLES / "ladder-b.toml", {"match.boards_max": 4})
    assert match_usd(half, "claude-opus-5-5", "gpt-5-mini") == pytest.approx(
        match_usd(cfg, "claude-opus-5-5", "gpt-5-mini") / 2
    )
