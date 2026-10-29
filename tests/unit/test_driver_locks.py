"""Arch round 3 #6: a second series/tournament driver on the same id fails fast with MatchLockedError."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.app.container import build_driver_lock
from claude_duel.app.series import play_series
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings
from claude_duel.domain import series as sr
from claude_duel.domain import tournament as tr
from claude_duel.domain.errors import MatchLockedError
from claude_duel.domain.models import AgentSpec, Rules
from claude_duel.observability import InMemoryMetrics


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1, challenges_per_round=1))


def test_concurrent_series_driver_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with build_driver_lock(settings, "s1", "series"), pytest.raises(MatchLockedError, match=r"s1\.series\.lock"):
        play_series(settings, "s1", sr.SeriesRule(games=1), InMemoryMetrics())
    assert play_series(settings, "s1", sr.SeriesRule(games=1), InMemoryMetrics()).done()  # released


def test_concurrent_tournament_driver_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    rule = tr.TournamentRule(entrants=tuple(AgentSpec(name=f"e{i}", skill=i) for i in range(3)))
    with (
        build_driver_lock(settings, "t1", "tournament"),
        pytest.raises(MatchLockedError, match=r"t1\.tournament\.lock"),
    ):
        play_tournament(settings, "t1", rule, InMemoryMetrics())
