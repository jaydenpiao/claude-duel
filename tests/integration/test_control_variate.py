"""Control variate: equal scripted bots replay each board's seed at $0; reported only, deterministic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_duel.app.container import build_duplicate_log
from claude_duel.app.control_variate import adjust
from claude_duel.app.duplicate import play_duplicate
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain.models import AgentSpec, Rules
from claude_duel.observability import InMemoryMetrics

RULE = dp.DuplicateRule(boards_min=2, boards_max=2)


def _settings(root: Path) -> Settings:
    agents = (AgentSpec(name="alice", skill=5), AgentSpec(name="bob", skill=1))
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1, challenges_per_round=1), agents=agents)


def test_adjusts_every_scored_board_and_leaves_the_result(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    played = play_duplicate(settings, "cv1", RULE, InMemoryMetrics())
    before = build_duplicate_log(settings).read("cv1")
    res = adjust(settings, "cv1")
    assert len(res.raw) == len(res.baseline) == len(res.adjusted) == len(played.boards)
    assert res.adjusted == [r - b for r, b in zip(res.raw, res.baseline, strict=True)]
    assert 0 <= res.p_value <= 1
    assert build_duplicate_log(settings).read("cv1") == before  # reported only
    assert adjust(settings, "cv1") == res  # deterministic and resumable


def test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    play_duplicate(settings, "cv2", RULE, InMemoryMetrics())
    monkeypatch.setattr("claude_duel.cli.load_settings", lambda path: settings)
    assert main(["control-variate", "--duplicate-id", "cv2"]) == 0
    assert "adjusted margin" in capsys.readouterr().out
    assert main(["control-variate", "--duplicate-id", "cv2", "--format", "json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) >= {"raw", "baseline", "adjusted", "p_value", "ci95"}
