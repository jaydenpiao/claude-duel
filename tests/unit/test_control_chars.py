"""Model-written text never reaches the terminal or card with control characters."""

from __future__ import annotations

import io

import pytest
from pydantic import ValidationError
from rich.console import Console

from claude_duel.domain.flags import printable, scrub
from claude_duel.domain.models import ChallengeSpec
from claude_duel.presentation.arena import ArenaView
from claude_duel.presentation.card import CardData, card_svg
from claude_duel.presentation.commentary import Commentary
from tests.helpers import forfeit_events

EVIL = "Maze \x1b]52;c;ZWNobyBwd25lZA==\x07\x1b[2J\x1b[H\u202escore: alice 99\x9b"


def test_printable_drops_controls_keeps_layout() -> None:
    assert printable("a\tb\nc\x1b[2J\x7f\x85\u2066d") == "a\tb\nc[2Jd"
    assert "\x1b" not in scrub(EVIL)
    assert "\u202e" not in scrub(EVIL)


@pytest.mark.parametrize("title", [EVIL, "a\nb", "a\u2067b"])
def test_title_rejects_control_chars(title: str) -> None:
    with pytest.raises(ValidationError):
        ChallengeSpec(title=title, description="d", source="print(1)", reference_payload="x" * 12, difficulty=3)


def test_commentary_has_no_esc() -> None:
    out = io.StringIO()
    view = Commentary(out, "m", 33.0, lambda: 0.0)
    for e in forfeit_events(EVIL, EVIL):
        view(e)
    assert "\x1b" not in out.getvalue()
    assert "score: alice 99" in out.getvalue()


def test_arena_has_no_esc() -> None:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=100)
    view = ArenaView(console, "m", 50.0, lambda: 0.0, pace=0.001)
    for e in forfeit_events(EVIL, EVIL):
        view(e)
    view.close()
    assert "\x1b" not in buf.getvalue()
    assert "\x1b" not in (view.model.banner or "")


def test_card_has_no_esc() -> None:
    data = CardData(
        match_id="m",
        players=[("alice", "a", 1), ("bob", "b", 0)],
        rounds="1/1",
        headline=EVIL,
        costs=[EVIL, EVIL],
        spend="$0",
        verified=True,
        digest="0123456789abcdef",
    )
    svg = card_svg(data)
    assert "\x1b" not in svg
    assert "\x07" not in svg
