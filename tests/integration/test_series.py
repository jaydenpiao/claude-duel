"""Best-of-N series (#38): early stop, alternating sides, derived seeds, resume and verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_duel.app import budget
from claude_duel.app.container import build_event_log, build_series_log, build_store
from claude_duel.app.series import play_series, verify_series
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain import series as sr
from claude_duel.domain.errors import BudgetExceededError, EventLogCorruptError
from claude_duel.domain.events import initial_state
from claude_duel.domain.models import AgentSpec, Attack, Event, Rules
from claude_duel.observability import InMemoryMetrics

AGENTS = (AgentSpec(name="alice", skill=5), AgentSpec(name="bob", skill=0))


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1), agents=AGENTS)


def test_bo3_stops_after_two_nil_with_alternating_sides_and_seeds(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = play_series(settings, "s1", sr.SeriesRule(games=3), InMemoryMetrics())
    assert state.done()
    assert [g.winner for g in state.games] == ["alice", "alice"]
    assert state.winner() == "alice"
    log = build_event_log(settings)
    for n in (1, 2):
        created = initial_state(f"s1-g{n}", log.read(f"s1-g{n}")[0])
        assert [a.name for a in created.agents] == [a.name for a in sr.game_agents(AGENTS, n)]
        assert created.seed == sr.game_seed(state.seed, n)
    assert [a.name for a in sr.game_agents(AGENTS, 2)] == ["bob", "alice"]
    assert not (tmp_path / "s1-g3.json").exists()
    again = play_series(settings, "s1", sr.SeriesRule(games=5), InMemoryMetrics())
    assert again.games == state.games  # resume keeps the saved rule; nothing is replayed or re-paid
    assert again.rule.games == 3
    assert verify_series(settings, "s1").consistent


def test_verify_flags_a_result_that_does_not_recompute(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    play_series(settings, "s2", sr.SeriesRule(games=1), InMemoryMetrics())
    match = build_store(settings).load("s2-g1")
    match.scores = {"alice": 0, "bob": 1}
    build_store(settings).save(match)
    report = verify_series(settings, "s2")
    assert not report.consistent
    assert "game 1: result" in report.mismatches


def test_series_cap_stops_before_the_next_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(budget.DEFAULT_PRICES, budget.FAKE_MODEL, (1.0, 1.0))
    settings = _settings(tmp_path)
    with pytest.raises(BudgetExceededError, match="series cost"):
        play_series(settings, "s3", sr.SeriesRule(games=3, series_max_usd=1e-9), InMemoryMetrics())
    assert len(sr.fold("s3", build_series_log(settings).read("s3")).games) == 1


def test_tie_goes_to_fewer_attack_tokens_then_draw(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    play_series(settings, "s4", sr.SeriesRule(games=1), InMemoryMetrics())
    match = build_store(settings).load("s4-g1")
    match.scores = {"alice": 3, "bob": 3}
    match.attacks = {
        "x": Attack(challenge_id="x", attacker="alice", solved=True, attempts=1, tokens_used=9, reason=""),
        "y": Attack(challenge_id="y", attacker="bob", solved=True, attempts=1, tokens_used=5, reason=""),
    }
    assert sr.game_winner(match) == "bob"
    match.attacks = {}
    assert sr.game_winner(match) is None
    drawn = sr.SeriesState(series_id="d", seed="s", rule=sr.SeriesRule(games=1), agents=AGENTS)
    drawn.games.append(sr.Game(game_no=1, match_id="d-g1", winner=None, scores={}))
    assert drawn.done()
    assert drawn.winner() is None


def test_bad_logs_and_rules_are_rejected() -> None:
    with pytest.raises(ValueError, match="odd"):
        sr.SeriesRule(games=4)
    with pytest.raises(EventLogCorruptError):
        sr.fold("z", [])
    created = Event(seq=0, type=sr.SERIES_CREATED, trace_id="z", data={"seed": "s", "rule": {}, "agents": []})
    created = created.model_copy(update={"data": {**created.data, "agents": [a.model_dump() for a in AGENTS]}})
    game = {"game_no": 2, "match_id": "z-g2", "winner": None, "scores": {}}
    with pytest.raises(EventLogCorruptError, match="out of order"):
        sr.fold("z", [created, Event(seq=1, type=sr.GAME_FINISHED, trace_id="z", data=game)])
    with pytest.raises(EventLogCorruptError, match="unknown"):
        sr.fold("z", [created, Event(seq=1, type="nope", trace_id="z", data={})])


def test_cli_series(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "duel.toml"
    cfg.write_text('sandbox = "subprocess"\n[rules]\nrounds = 1\n')
    assert main(["--config", str(cfg), "series", "--series-id", "c1", "--games", "1", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["verified"]
    assert out["best_of"] == 1
    assert main(["--config", str(cfg), "series", "--series-id", "c1", "--games", "1"]) == 0
    assert "series verified ✓" in capsys.readouterr().out
    assert main(["--config", str(cfg), "series", "--games", "2"]) == 2
