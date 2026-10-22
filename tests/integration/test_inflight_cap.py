"""series and tournament caps count every game from the ledger, so they stop inside a running series."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.app import budget
from claude_duel.app.container import build_ledger, build_series_log
from claude_duel.app.series import play_series
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings
from claude_duel.domain import series as sr
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import AgentSpec, Rules
from claude_duel.domain.tournament import TournamentRule
from claude_duel.observability import InMemoryMetrics

AGENTS = (AgentSpec(name="alice", skill=5), AgentSpec(name="bob", skill=0), AgentSpec(name="carol", skill=3))


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1), agents=AGENTS[:2])


def test_tournament_stops_inside_a_series(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(budget.DEFAULT_PRICES, budget.FAKE_MODEL, (1.0, 1.0))
    settings = _settings(tmp_path)
    rule = TournamentRule(entrants=AGENTS, series=sr.SeriesRule(games=5), tournament_max_usd=1e-9)
    with pytest.raises(BudgetExceededError, match="tournament cost"):
        play_tournament(settings, "t1", rule, InMemoryMetrics())
    (sid,) = {p.name.removesuffix(".series.jsonl") for p in tmp_path.glob("*.series.jsonl")}
    games = sr.fold(sid, build_series_log(settings).read(sid)).games
    assert len(games) == 1  # one game played, then the tournament cap stopped the series
    assert build_ledger(settings).spent_usd("t1-") > 0


def test_failed_game_spend_counts_toward_the_series(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    build_ledger(settings).record("s1-g1", "fake-1", 10, 0.5)  # e.g. a game that failed before game_finished
    with pytest.raises(BudgetExceededError, match="series cost"):
        play_series(settings, "s1", sr.SeriesRule(games=3, series_max_usd=0.25), InMemoryMetrics())
    assert build_ledger(settings).spent_usd("s1-") == 0.5
    assert build_ledger(settings).spent_usd("s2-") == 0
