"""Series and tournament logs pin rules and limits; resume ignores config drift."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_duel.app.budget import BudgetLimits
from claude_duel.app.container import build_series_log, build_store
from claude_duel.app.series import play_series, verify_series
from claude_duel.app.session import play
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings, load_settings
from claude_duel.domain import series as sr
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


def test_ledger_overlay_series_and_tournament_share_the_heat_pin(tmp_path: Path) -> None:
    """A ``[rules.ledger]`` table replaces the preset ledger. The driver must pin caps before the log."""
    path = tmp_path / "overlay.toml"
    path.write_text('[rules]\nmode = "ledger"\n\n[rules.ledger]\nheat_window = 2\n', encoding="utf-8")
    loaded = load_settings(path, env={})
    assert loaded.rules.ledger is not None
    assert loaded.rules.ledger.heat_token_cap is None
    assert loaded.rules.ledger.keystone is False
    rules = loaded.rules.model_copy(update={"rounds": 1, "challenges_per_round": 1})
    settings = loaded.model_copy(
        update={
            "store_dir": tmp_path,
            "sandbox": "subprocess",
            "agents": AGENTS,
            "rules": rules,
            "budget": BudgetLimits(run_max_calls=400),
        }
    )
    play_series(settings, "overlay", SeriesRule(games=1), InMemoryMetrics())
    report = verify_series(settings, "overlay")
    assert report.consistent, report.mismatches
    match = build_store(settings).load("overlay-g1")
    assert match.rules.ledger is not None
    assert (match.rules.ledger.heat_token_cap, match.rules.ledger.heat_ref_cap, match.rules.ledger.heat_compare) == (
        256,
        48,
        "full",
    )
    logged = sr.fold("overlay", build_series_log(settings).read("overlay")).rules
    assert logged == match.rules
    tour = settings.model_copy(update={"store_dir": tmp_path / "tour"})
    entrants = (*AGENTS, AgentSpec(name="carol", skill=1))
    state = play_tournament(tour, "t", TournamentRule(entrants=entrants), InMemoryMetrics())
    assert state.rules == logged
    for pairing in state.pairings:
        if not pairing.series_id:
            continue
        child = sr.fold(pairing.series_id, build_series_log(tour).read(pairing.series_id))
        assert child.rules == state.rules
        for game in child.games:
            played = build_store(tour).load(game.match_id)
            assert played.rules == state.rules


def test_tournament_pins_rules_for_every_series(tmp_path: Path) -> None:
    entrants = (*AGENTS, AgentSpec(name="carol", skill=1))
    rule = TournamentRule(entrants=entrants)
    state = play_tournament(_settings(tmp_path), "t1", rule, InMemoryMetrics())
    assert state.rules == ORIGINAL
    again = play_tournament(_settings(tmp_path, rules=preset("quick")), "t1", rule, InMemoryMetrics())
    assert again.rules == ORIGINAL
