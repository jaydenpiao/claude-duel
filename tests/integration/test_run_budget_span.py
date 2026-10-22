"""R3-1: the run spend cap spans every game of a series or tournament invocation, not one game."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.app.budget import BudgetLimits
from claude_duel.app.series import play_series
from claude_duel.app.session import play
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import AgentSpec, Rules
from claude_duel.domain.series import SeriesRule
from claude_duel.domain.tournament import TournamentRule
from claude_duel.observability import InMemoryMetrics

AGENTS = (AgentSpec(name="alice", skill=5), AgentSpec(name="bob", skill=0))


def _settings(root: Path, run_calls: int = 200) -> Settings:
    budget = BudgetLimits(run_max_calls=run_calls)
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1), agents=AGENTS, budget=budget)


def _calls_per_game(root: Path) -> int:
    return play(_settings(root), "probe", InMemoryMetrics()).usage.calls


def test_series_stops_at_the_run_cap(tmp_path: Path) -> None:
    per_game = _calls_per_game(tmp_path / "probe")
    settings = _settings(tmp_path, run_calls=per_game + 1)  # room for one game, not two
    with pytest.raises(BudgetExceededError, match="run calls"):
        play_series(settings, "s1", SeriesRule(games=3), InMemoryMetrics())


def test_tournament_stops_at_the_run_cap(tmp_path: Path) -> None:
    per_game = _calls_per_game(tmp_path / "probe")
    entrants = (*AGENTS, AgentSpec(name="carol", skill=3))
    settings = _settings(tmp_path, run_calls=per_game + 1)
    with pytest.raises(BudgetExceededError, match="run calls"):
        play_tournament(settings, "t1", TournamentRule(entrants=entrants), InMemoryMetrics())
