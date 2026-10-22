"""Sudden-death tiebreak (#38 follow-up): a tied game is settled by one extra 1-round match."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.app.container import build_event_log, build_store
from claude_duel.app.series import play_series, verify_series
from claude_duel.config import Settings
from claude_duel.domain import series as sr
from claude_duel.domain.events import initial_state
from claude_duel.domain.models import AgentSpec, MatchState, Rules
from claude_duel.observability import InMemoryMetrics

AGENTS = (AgentSpec(name="alice", skill=5), AgentSpec(name="bob", skill=0))
real_winner = sr.game_winner


def _tie_unless_decider(state: MatchState) -> str | None:
    return real_winner(state) if state.match_id.endswith("-sd") else None


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=2), agents=AGENTS)


def test_tied_game_goes_to_sudden_death(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "game_winner", _tie_unless_decider)
    settings = _settings(tmp_path)
    state = play_series(settings, "s1", sr.SeriesRule(games=1), InMemoryMetrics())
    (game,) = state.games
    assert game.sudden_death == "s1-g1-sd"
    assert game.winner == "alice"
    created = initial_state("s1-g1-sd", build_event_log(settings).read("s1-g1-sd")[0])
    assert created.rules.rounds == 1
    assert created.seed == sr.game_seed(state.seed, -1)
    assert build_store(settings).load("s1-g1").rules.rounds == 2
    assert verify_series(settings, "s1").consistent
    assert play_series(settings, "s1", sr.SeriesRule(games=1), InMemoryMetrics()).games == state.games


def test_sudden_death_can_be_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "game_winner", _tie_unless_decider)
    settings = _settings(tmp_path)
    state = play_series(settings, "s2", sr.SeriesRule(games=1, sudden_death=False), InMemoryMetrics())
    assert state.games[0].winner is None
    assert state.games[0].sudden_death == ""
    assert not (tmp_path / "s2-g1-sd.json").exists()


def test_old_game_records_still_load() -> None:
    old = {"game_no": 1, "match_id": "x-g1", "winner": None, "scores": {}}
    assert sr.Game.model_validate(old).sudden_death == ""
