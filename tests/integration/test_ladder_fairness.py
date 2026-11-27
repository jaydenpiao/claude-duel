"""A vendor policy block is an outcome of its pairing (the ladder goes on), and heat history is scoped to the
current pairing: a duplicate match never scores novelty against another pairing, step or run in the same store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import ladder as ladder_mod
from claude_duel.app import ladder_config as lc
from claude_duel.app.budget import POLICY_BLOCKS_TO_PAUSE
from claude_duel.app.container import build_event_log, build_store
from claude_duel.app.duplicate import play_duplicate
from claude_duel.app.ladder import Comparison, by_vendor, play_ladder, verify_ladder
from claude_duel.app.ladder_config import agents
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain.errors import CyberPolicyError
from claude_duel.observability import InMemoryMetrics
from claude_duel.presentation.ladder import comparison_lines, comparison_view

FAKE = Path(__file__).resolve().parents[2] / "examples" / "ladder-fake.toml"


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess")


def test_a_policy_block_out_of_a_step_pauses_it_and_a_rerun_resumes_the_same_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S18-5: a block that stops a step pauses the ladder at once; the step is not logged as "blocked" (nobody steps
    down) and a rerun resumes it."""
    real, calls = ladder_mod.play_duplicate, []

    def block_first(settings: Settings, duplicate_id: str, *a: Any) -> Any:
        calls.append(duplicate_id)
        if len(calls) == 1:
            raise CyberPolicyError("openai cyber_policy: access limited; not retried")
        return real(settings, duplicate_id, *a)

    monkeypatch.setattr(ladder_mod, "play_duplicate", block_first)
    cfg = lc.load(FAKE)
    state = play_ladder(_settings(tmp_path), "lad-block", cfg, InMemoryMetrics())
    assert state.steps == []
    assert state.paused == 1
    assert not state.done()
    assert verify_ladder(_settings(tmp_path), "lad-block").consistent
    again = play_ladder(_settings(tmp_path), "lad-block", cfg, InMemoryMetrics())
    assert calls[1] == calls[0]  # the same step resumed
    assert again.done()
    assert all(s.result != "blocked" for s in again.steps)


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


def _blocked_events(settings: Settings, ladder_id: str, steps: int) -> list[dict[str, Any]]:
    log = build_event_log(settings)
    return [
        e.data
        for n in range(1, steps + 2)
        for mid in (f"{ladder_id}-s{n}-b{k}{h}" for k in range(1, 40) for h in "ab")
        for e in log.read(mid)
        if e.type == "call_charged" and e.data.get("policy_blocked")
    ]


def test_one_policy_block_is_a_spent_try_at_zero_dollars_and_the_step_keeps_its_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S17-3/S18-5: one cyber_policy block on an attack is that agent's spent try ($0, no forfeit, no pause); the
    blocked attempt carries no output tokens and replay recomputes the same score."""
    _block_attacks(monkeypatch, 1)
    settings = _settings(tmp_path)
    state = play_ladder(settings, "lad-s173", lc.load(FAKE), InMemoryMetrics())
    assert state.steps[0].result != "blocked"
    assert state.paused == 0
    blocked = _blocked_events(settings, "lad-s173", len(state.steps))
    assert len(blocked) == 1
    assert blocked[0]["refused"] is True
    assert blocked[0]["usd"] == 0.0
    assert blocked[0].get("output_tokens", 0) == 0
    store = build_store(settings)
    halves = [f"{s.duplicate_id}-b{k}{h}" for s in state.steps for k in range(1, 40) for h in "ab"]
    refused = [
        t
        for mid in halves
        if build_event_log(settings).read(mid)
        for tries in store.load(mid).attempts.values()
        for t in tries
        if t.refused
    ]
    assert len(refused) == 1
    assert refused[0].tokens == 0  # no output tokens: nothing was generated
    assert verify_ladder(settings, "lad-s173").consistent  # every half replays to the same score
    assert ladder_mod.policy_blocks(settings, state) == {"fake-1": 1}


def test_a_vendors_second_block_in_a_step_pauses_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S18-5 (a): an account-limited vendor (every attack blocked) gets one more request after the first block,
    then the ladder pauses mid-step; the decided boards are kept and a rerun resumes the step."""
    seen = _block_attacks(monkeypatch, 10_000)
    settings = _settings(tmp_path)
    cfg = lc.load(FAKE)
    state = play_ladder(settings, "lad-s185", cfg, InMemoryMetrics())
    assert len(seen) == POLICY_BLOCKS_TO_PAUSE  # no further request to the limited org
    assert state.steps == []
    assert state.paused == 1
    monkeypatch.undo()
    again = play_ladder(settings, "lad-s185", cfg, InMemoryMetrics())
    assert again.done()
    assert verify_ladder(settings, "lad-s185").consistent


def test_a_blocked_build_is_never_a_forfeit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S18-5 (b): a block on a build pauses the step instead of forfeiting the author."""
    real = FakeProvider.complete

    def complete(self: FakeProvider, request: Any) -> Any:
        if request.purpose == "build":
            raise CyberPolicyError("openai cyber_policy: access limited; not retried")
        return real(self, request)

    monkeypatch.setattr(FakeProvider, "complete", complete)
    settings = _settings(tmp_path)
    state = play_ladder(settings, "lad-s185b", lc.load(FAKE), InMemoryMetrics())
    assert state.paused == 1
    store = build_store(settings)
    for h in "ab":
        if build_event_log(settings).read(f"lad-s185b-s1-b1{h}"):
            assert all(c.forfeit_reason is None for c in store.load(f"lad-s185b-s1-b1{h}").challenges.values())


def test_policy_blocks_are_shown_per_model_and_vendor_only_when_present() -> None:
    blocks = {"gpt-6-luna": 2, "gpt-5-nano": 1}
    cmp = Comparison([], "agree", (blocks, by_vendor(blocks)))
    want = "policy blocks (spent tries, $0): gpt-6-luna 2, gpt-5-nano 1 · by vendor: openai 3"
    assert comparison_lines(cmp)[-1] == want
    assert comparison_view(cmp)["policy_blocked"] == {"by_model": blocks, "by_vendor": {"openai": 3}}
    assert not any("policy" in line for line in comparison_lines(Comparison([], "agree")))
