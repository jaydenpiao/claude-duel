"""Duplicate play end to end with fake agents ($0): mirrored halves, resume, verify, cap, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_duel.app import budget
from claude_duel.app.container import build_event_log, build_store
from claude_duel.app.duplicate import play_duplicate, verify_duplicate
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain.events import initial_state
from claude_duel.domain.models import AgentSpec, Rules
from claude_duel.observability import InMemoryMetrics

AGENTS = (AgentSpec(name="strong", skill=5), AgentSpec(name="weak", skill=0))
RULE = dp.DuplicateRule(boards_min=2, boards_max=4)


def _settings(root: Path, agents: tuple[AgentSpec, AgentSpec] = AGENTS) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1), agents=agents)


def test_boards_mirror_seats_and_seeds_then_resume_and_verify(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = play_duplicate(settings, "d1", RULE, InMemoryMetrics())
    assert state.done()
    assert 2 <= len(state.boards) <= 4
    log = build_event_log(settings)
    for b in state.boards:
        first, second = (initial_state(m, log.read(m)[0]) for m in b.halves)
        assert [a.name for a in first.agents] == ["strong", "weak"]
        assert [a.name for a in second.agents] == ["weak", "strong"]
        assert first.seed == second.seed == dp.board_seed(state.seed, b.board_no)
    assert all(b.diff >= 0 for b in state.boards)
    again = play_duplicate(settings, "d1", dp.DuplicateRule(boards_min=8, boards_max=8), InMemoryMetrics())
    assert again.boards == state.boards  # resume keeps the saved rule; nothing is replayed or re-paid
    assert verify_duplicate(settings, "d1").consistent


def test_verify_flags_a_board_that_does_not_recompute(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = play_duplicate(settings, "d3", RULE, InMemoryMetrics())
    half = build_store(settings).load(state.boards[0].halves[0])
    half.scores = {"strong": 0, "weak": 99}
    build_store(settings).save(half)
    report = verify_duplicate(settings, "d3")
    assert not report.consistent
    assert "board 1: result" in report.mismatches


def test_cap_stops_before_a_board_that_could_pass_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(budget.DEFAULT_PRICES, budget.FAKE_MODEL, (1.0, 1.0))
    settings = _settings(tmp_path)
    first = play_duplicate(settings, "d4", dp.DuplicateRule(boards_min=1, boards_max=1), InMemoryMetrics())
    cost = first.boards[0].usd
    assert cost > 0
    rule = dp.DuplicateRule(boards_min=4, boards_max=4, match_max_usd=cost * 1.5)
    state = play_duplicate(settings, "d5", rule, InMemoryMetrics())
    assert state.capped
    assert len(state.boards) == 1
    assert state.spent_usd() <= rule.match_max_usd


def test_cli_prints_the_board_table_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = tmp_path / "duel.toml"
    cfg.write_text(
        f'store_dir = "{tmp_path}"\nsandbox = "subprocess"\n[rules]\nrounds = 1\n'
        '[[agents]]\nname = "strong"\nskill = 5\n[[agents]]\nname = "weak"\nskill = 0\n'
    )
    assert (
        main(["--config", str(cfg), "duplicate", "--duplicate-id", "c1", "--boards-min", "2", "--boards-max", "2"]) == 0
    )
    out = capsys.readouterr().out
    assert "duplicate c1: strong vs weak" in out
    assert "board 1:" in out
    assert "verified ✓" in out
    assert main(["--config", str(cfg), "duplicate", "--duplicate-id", "c1", "--format", "json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["sides"] == ["strong", "weak"]
    assert view["verified"] is True
    assert len(view["boards"]) == 2
