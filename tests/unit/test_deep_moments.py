"""Deep layer phase 1: moment detector and the arena GHOSTS row (design-deep-layer.md section 9, tests 7-8)."""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pytest
from rich.console import Console

from claude_duel.app import budget
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain.flags import FLAG_PATTERN
from claude_duel.domain.models import Event, MatchState
from claude_duel.domain.modes import preset
from claude_duel.domain.moments import GHOST_STRIKE, moments
from claude_duel.observability import InMemoryMetrics
from claude_duel.presentation.arena import ArenaView, frame

SNAPSHOTS = Path(__file__).parent.parent / "snapshots"


def _text(view: ArenaView, width: int) -> str:
    c = Console(file=io.StringIO(), width=width, force_terminal=False, no_color=True, record=True)
    c.print(frame(view.model, width))
    return c.export_text()


class Tap(ArenaView):
    """Arena view that keeps every event and renders a frame after each one."""

    def __init__(self, console: Console) -> None:
        super().__init__(console, "deep", 50.0, lambda: 0.0)
        self.events: list[Event] = []
        self.frames: list[str] = []

    def __call__(self, event: Event) -> None:
        super().__call__(event)
        self.events.append(event)
        self.frames.append(_text(self, 100))


@pytest.fixture(scope="module")
def deep(tmp_path_factory: pytest.TempPathFactory) -> tuple[Tap, MatchState]:
    console = Console(file=io.StringIO(), width=100, force_terminal=False, no_color=True)
    tap = Tap(console)
    rules = preset("ledger_deep").model_copy(update={"rounds": 2})
    settings = Settings(store_dir=tmp_path_factory.mktemp("deep"), sandbox="subprocess", rules=rules)
    with pytest.MonkeyPatch.context() as mp:  # priced like gpt-5-nano, so the $ figures are real, not $0.0000
        mp.setitem(budget.DEFAULT_PRICES, budget.FAKE_MODEL, budget.DEFAULT_PRICES["gpt-5-nano"])
        state = play(settings, "deep", InMemoryMetrics(), tap)
    tap.close()
    return tap, state


def test_ghost_strike_moments_are_exactly_the_ghost_hits(deep: tuple[Tap, MatchState]) -> None:
    tap, _ = deep
    hits = [(e.data["target"], e.data["ghost_id"]) for e in tap.events if e.type == ev.GHOST_RUN and e.data["hit"]]
    found = moments(tap.events)
    assert hits
    assert [(m.subject, m.by) for m in found] == hits
    assert {m.kind for m in found} == {GHOST_STRIKE}


@pytest.mark.parametrize("width", [100, 80])
def test_deep_arena_golden_frame(deep: tuple[Tap, MatchState], width: int) -> None:
    tap, _ = deep
    text = _text(tap, width)
    path = SNAPSHOTS / f"arena_deep_{width}.txt"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")
    assert "GHOSTS r2:" in text
    assert all(len(line) <= width for line in text.splitlines())


def test_arena_tokens_and_dollars_are_call_charged_sums(deep: tuple[Tap, MatchState]) -> None:
    tap, _ = deep
    text = _text(tap, 200)
    for name in ("alice", "bob"):
        mine = [e.data for e in tap.events if e.type == ev.CALL_CHARGED and e.data["agent"] == name]
        tokens_in = sum(int(d.get("input_tokens", 0)) for d in mine)  # type: ignore[call-overload]
        usd = sum(float(d["usd"]) for d in mine)  # type: ignore[arg-type]
        assert usd > 0
        assert f"{name}: in {tokens_in:,} · " in text
        assert f"${usd:.4f}" in text


def test_no_flag_or_payload_bytes_in_any_frame(deep: tuple[Tap, MatchState]) -> None:
    """Every payload, short ones included (the marquee exploit is 4 bytes), as a whole token in every frame.

    Opponent prompts are covered by the ledger differential tests (#192 stakes, #193 keystone)."""
    tap, state = deep
    payloads = sorted({a.payload.strip() for tries in state.attempts.values() for a in tries if a.payload.strip()})
    chrome = tap.frames[0]  # rendered before any payload existed: words like "duel" there are the app's own
    payloads = [p for p in payloads if not re.search(rf"(?<!\w){re.escape(p)}(?!\w)", chrome)]
    assert any(len(p) < 5 for p in payloads)
    alts = "|".join(re.escape(p) for p in sorted(payloads, key=len, reverse=True))
    pattern = re.compile(rf"(?<!\w)(?:{alts})(?!\w)")
    for text in tap.frames:
        assert not FLAG_PATTERN.search(text)
        assert not pattern.search(text), pattern.search(text)


def test_ghost_row_is_hidden_without_ghosts(tmp_path: Path) -> None:
    console = Console(file=io.StringIO(), width=100, force_terminal=False, no_color=True)
    view = ArenaView(console, "plain", 50.0, lambda: 0.0)
    rules = preset("ledger").model_copy(update={"rounds": 1})
    play(Settings(store_dir=tmp_path, sandbox="subprocess", rules=rules), "plain", InMemoryMetrics(), view)
    view.close()
    assert "GHOSTS" not in _text(view, 100)


def test_arena_logs_each_terrain_draw_and_heat_line(deep: tuple[Tap, MatchState]) -> None:
    tap, _ = deep
    drawn = [e for e in tap.events if e.type == ev.TERRAIN_DRAWN]
    heat = [line for e in tap.events if e.type == ev.ROUND_SCORED for line in e.data.get("heat", {}).values() if line]
    seen = "\n".join(tap.frames)
    assert drawn
    assert all(f"terrain r{e.data['round']}:" in seen for e in drawn)
    assert heat
    assert all(f"ran hot -{line}" in seen for line in heat)
