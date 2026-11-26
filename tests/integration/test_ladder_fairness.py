"""A vendor policy block is an outcome of its pairing (the ladder goes on), and heat history is scoped to the
current pairing: a duplicate match never scores novelty against another pairing, step or run in the same store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_duel.app import ladder as ladder_mod
from claude_duel.app import ladder_config as lc
from claude_duel.app.container import build_store
from claude_duel.app.duplicate import play_duplicate
from claude_duel.app.ladder import compare_ladders, play_ladder, verify_ladder
from claude_duel.app.ladder_config import agents
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain.errors import CyberPolicyError
from claude_duel.observability import InMemoryMetrics

FAKE = Path(__file__).resolve().parents[2] / "examples" / "ladder-fake.toml"


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess")


def test_a_cyber_policy_block_is_a_blocked_step_and_the_ladder_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, calls = ladder_mod.play_duplicate, []

    def block_first(settings: Settings, duplicate_id: str, *a: Any) -> Any:
        calls.append(duplicate_id)
        if len(calls) == 1:
            raise CyberPolicyError("openai cyber_policy: access limited; not retried")
        return real(settings, duplicate_id, *a)

    monkeypatch.setattr(ladder_mod, "play_duplicate", block_first)
    cfg = lc.load(FAKE)
    state = play_ladder(_settings(tmp_path), "lad-block", cfg, InMemoryMetrics())
    assert state.steps[0].result == "blocked"
    assert len(state.steps) >= 2  # not paused: the next pairing was played
    assert state.done()
    assert state.tiers() != (0, 0)
    monkeypatch.setattr(ladder_mod, "play_duplicate", real)
    assert verify_ladder(_settings(tmp_path), "lad-block").consistent
    again = play_ladder(_settings(tmp_path), "lad-block", cfg, InMemoryMetrics())
    assert again.steps == state.steps  # resume keeps the blocked step; nothing is retried
    first = compare_ladders(state, again).matchups[0]
    assert first.verdict == "too close to call"  # a blocked pairing never counts as agreement


def _refs(settings: Settings, half: str) -> list[str]:
    """Every heat reference source the half was created with, over both agents."""
    return [src for sources in build_store(settings).load(half).heat_refs.values() for src in sources]


def _built(settings: Settings, *halves: str) -> set[str]:
    store = build_store(settings)
    return {c.spec.source for h in halves for c in store.load(h).challenges.values() if c.spec is not None}


def test_heat_history_is_scoped_to_the_current_pairing(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"rules": lc.load(FAKE).rules(), "agents": agents("fake-1", "fake-1")}
    )
    rule = dp.DuplicateRule(boards_min=2, boards_max=2, stop="none")
    play_duplicate(settings, "pair1", rule, InMemoryMetrics())
    play_duplicate(settings, "pair2", rule, InMemoryMetrics())
    assert _refs(settings, "pair1-b2a")  # heat is on: a pairing's later halves see its earlier ones
    assert _refs(settings, "pair2-b1a") == []  # pair1's halves are in the store, but another pairing never counts
    later = _refs(settings, "pair2-b2a")
    assert set(later) <= _built(settings, "pair2-b1a", "pair2-b1b")  # only this pairing's own earlier halves


def test_two_blocks_in_a_row_pause_the_ladder_and_a_rerun_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ladder_mod.play_duplicate

    def block(settings: Settings, duplicate_id: str, *a: Any) -> Any:
        raise CyberPolicyError("openai cyber_policy: access limited; not retried")

    plan = tmp_path / "three.toml"
    plan.write_text(
        FAKE.read_text()
        .replace('a = ["fake-1", "fake-1"]', 'a = ["fake-1", "fake-1", "fake-1"]')
        .replace('b = ["fake-1", "fake-1"]', 'b = ["fake-1", "fake-1", "fake-1"]')
    )
    cfg = lc.load(plan)
    monkeypatch.setattr(ladder_mod, "play_duplicate", block)
    state = play_ladder(_settings(tmp_path), "lad-pause", cfg, InMemoryMetrics())
    assert [s.result for s in state.steps] == ["blocked", "blocked"]
    assert state.paused == 1  # paused, not stepped down past the vendor's whole side
    assert not state.done()
    monkeypatch.setattr(ladder_mod, "play_duplicate", real)
    assert verify_ladder(_settings(tmp_path), "lad-pause").consistent
    again = play_ladder(_settings(tmp_path), "lad-pause", cfg, InMemoryMetrics())
    assert again.done()
    assert again.steps[:2] == state.steps
    assert len(again.steps) == 3
