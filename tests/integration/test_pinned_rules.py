"""Arch round 3 (major): series and tournament logs pin rules and limits; resume ignores config drift."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_duel.app.budget import BudgetLimits
from claude_duel.app.container import build_store
from claude_duel.app.series import play_series, verify_series
from claude_duel.app.session import play
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import AgentSpec, Rules, SandboxLimits
from claude_duel.domain.modes import preset
from claude_duel.domain.series import SeriesRule
from claude_duel.domain.tournament import TournamentRule
from claude_duel.observability import InMemoryMetrics

AGENTS = (AgentSpec(name="alice", skill=3), AgentSpec(name="bob", skill=3))
ORIGINAL = Rules(rounds=1)


def _settings(root: Path, rules: Rules = ORIGINAL, run_calls: int = 200) -> Settings:
    budget = BudgetLimits(run_max_calls=run_calls)
    return Settings(store_dir=root, sandbox="subprocess", rules=rules, agents=AGENTS, budget=budget)


def test_series_resume_keeps_the_pinned_rules(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    per_game = play(_settings(tmp_path / "probe"), "probe", InMemoryMetrics()).usage.calls
    with pytest.raises(BudgetExceededError):  # stops after one game
        play_series(_settings(tmp_path, run_calls=per_game + 1), "s1", SeriesRule(games=3), InMemoryMetrics())
    drifted = _settings(tmp_path, rules=preset("quick").model_copy(update={"rounds": 2}))
    drifted = drifted.model_copy(update={"limits": SandboxLimits(wall_seconds=3.0)})
    with caplog.at_level(logging.WARNING):
        state = play_series(drifted, "s1", SeriesRule(games=3), InMemoryMetrics())
    assert "saved rules" in caplog.text
    assert len(state.games) >= 2
    store = build_store(drifted)
    for g in state.games:
        match = store.load(g.match_id)
        assert match.rules == ORIGINAL
        assert match.limits == SandboxLimits()
    assert verify_series(drifted, "s1").consistent


def test_verify_flags_a_game_with_other_rules(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    play_series(settings, "s2", SeriesRule(games=1), InMemoryMetrics())
    store = build_store(settings)
    match = store.load("s2-g1")
    store.save(match.model_copy(update={"rules": match.rules.model_copy(update={"max_attempts": 9})}))
    report = verify_series(settings, "s2")
    assert "game 1: rules differ from the pinned series rules" in report.mismatches


def test_tournament_pins_rules_for_every_series(tmp_path: Path) -> None:
    entrants = (*AGENTS, AgentSpec(name="carol", skill=1))
    rule = TournamentRule(entrants=entrants)
    state = play_tournament(_settings(tmp_path), "t1", rule, InMemoryMetrics())
    assert state.rules == ORIGINAL
    again = play_tournament(_settings(tmp_path, rules=preset("quick")), "t1", rule, InMemoryMetrics())
    assert again.rules == ORIGINAL
