"""A vendor policy block is an outcome of its pairing (the ladder goes on), and heat history is scoped to the
current pairing: a duplicate match never scores novelty against another pairing, step or run in the same store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import ladder as ladder_mod
from claude_duel.app import ladder_config as lc
from claude_duel.app.container import build_event_log, build_store
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


def _block_attacks(monkeypatch: pytest.MonkeyPatch, times: int) -> list[str]:
    """The fake vendor raises cyber_policy on its first ``times`` attack calls (as if opponent text baited it)."""
    real, seen = FakeProvider.complete, list[str]()

    def complete(self: FakeProvider, request: Any) -> Any:
        if request.purpose == "attack" and len(seen) < times:
            seen.append(request.prompt)
            raise CyberPolicyError("openai cyber_policy: access limited; not retried")
        return real(self, request)

    monkeypatch.setattr(FakeProvider, "complete", complete)
    return seen


def test_one_policy_block_is_a_refusal_and_the_step_keeps_its_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S17-3: a cyber_policy block on one call is that agent's refusal; the step is not "blocked" (no step-down for
    both sides, decided boards kept) and the block is logged, with whether the prompt held opponent text."""
    _block_attacks(monkeypatch, 1)
    settings = _settings(tmp_path)
    state = play_ladder(settings, "lad-s173", lc.load(FAKE), InMemoryMetrics())
    assert state.steps[0].result != "blocked"
    assert state.paused == 0
    log = build_event_log(settings)
    blocked = [
        e.data
        for s in state.steps
        for mid in (f"{s.duplicate_id}-b{k}{h}" for k in range(1, 40) for h in "ab")
        for e in log.read(mid)
        if e.type == "call_charged" and e.data.get("policy_blocked")
    ]
    assert len(blocked) == 1
    assert blocked[0]["refused"] is True
    assert blocked[0]["opponent_text"] is True  # the attack prompt fences the author's source
    assert blocked[0]["usd"] == 0.0


def test_repeated_policy_blocks_pause_the_ladder_after_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_attacks(monkeypatch, ladder_mod.POLICY_BLOCKS_TO_PAUSE)
    state = play_ladder(_settings(tmp_path), "lad-s173p", lc.load(FAKE), InMemoryMetrics())
    assert len(state.steps) == 1
    assert state.steps[0].result != "blocked"  # the verdict stands
    assert state.paused == 1
    assert verify_ladder(_settings(tmp_path), "lad-s173p").consistent
