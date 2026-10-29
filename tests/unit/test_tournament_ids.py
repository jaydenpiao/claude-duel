"""Arch round 3 #4: series ids come from entrant indexes, so look-alike names never share a series log."""

from __future__ import annotations

from pathlib import Path

from claude_duel.app.container import build_series_log
from claude_duel.app.tournament import play_tournament, series_id
from claude_duel.config import Settings
from claude_duel.domain import tournament as tr
from claude_duel.domain.models import AgentSpec, Event, Rules
from claude_duel.domain.series import SERIES_CREATED
from claude_duel.observability import InMemoryMetrics

NAMES = ("x_y", "x-y", "a", "b")


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1, challenges_per_round=1))


def test_lookalike_entrants_get_distinct_series(tmp_path: Path) -> None:
    rule = tr.TournamentRule(entrants=tuple(AgentSpec(name=n, skill=i % 6) for i, n in enumerate(NAMES)))
    state = play_tournament(_settings(tmp_path), "t", rule, InMemoryMetrics())
    sids = [p.series_id for p in state.pairings if p.series_id]
    assert len(sids) == len(set(sids)) == 6  # 4 entrants round robin; old ids repeat (x_y|x-y v a)
    assert all("-p" in s and "v" in s for s in sids)


def test_a_started_legacy_series_keeps_its_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    names = ["x_y", "z"]
    assert series_id(settings, "t", 1, names, ("x_y", "z")) == "t-r1-p0v1"
    created = Event(v=1, seq=0, type=SERIES_CREATED, trace_id="t-r1-x-y-z", data={"seed": "s", "rule": {}})
    build_series_log(settings).append("t-r1-x-y-z", created)
    assert series_id(settings, "t", 1, names, ("x_y", "z")) == "t-r1-x-y-z"
