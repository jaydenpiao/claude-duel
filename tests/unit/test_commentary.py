"""Commentary renderer: snapshot of a full fake match, plus each special line."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_duel.app.budget import BudgetLimits
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain.models import Event, Rules
from claude_duel.observability import InMemoryMetrics
from claude_duel.presentation.commentary import Commentary, scrub, snippet
from claude_duel.presentation.common import labels
from tests.helpers import FLAG, forfeit_events, match_events

SNAPSHOT = Path(__file__).parent.parent / "snapshots" / "commentary_fake_match.txt"


def _commentary(out: io.StringIO, match_id: str = "snap") -> Commentary:
    return Commentary(out, match_id, 33.0, lambda: 0.0031)


def test_full_match_snapshot(tmp_path: Path) -> None:
    out = io.StringIO()
    settings = Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(rounds=2))
    play(settings, "snap", InMemoryMetrics(), _commentary(out))
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(out.getvalue(), encoding="utf-8")
    assert out.getvalue() == SNAPSHOT.read_text(encoding="utf-8")


def test_resume_replays_history_then_continues(tmp_path: Path) -> None:
    settings = Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(rounds=1))
    play(settings, "m1", InMemoryMetrics())
    out = io.StringIO()
    play(settings, "m1", InMemoryMetrics(), _commentary(out, "m1"))
    assert "alice vs bob" in out.getvalue()
    assert "winner: " in out.getvalue()


def test_scrub_and_snippet() -> None:
    assert FLAG not in scrub(f"x {FLAG} y")
    assert scrub("key sk-abcdefgh12345678 ok") == "key <redacted> ok"
    assert scrub("secret " + "ab" * 32) == "secret <redacted>"
    lines = snippet("a\n\nb\nc\nd\ne\n")
    assert lines == ["a", "b", "c", "… (+2 lines)"]
    assert snippet("x" * 200)[0] == "x" * 72


def _state_events(*extra: Event) -> list[Event]:
    return match_events(*extra)


def _render(events: list[Event]) -> str:
    out = io.StringIO()
    c = _commentary(out, "m")
    for e in events:
        c(e)
    assert c.state is not None
    return out.getvalue()


def test_forfeit_rejected_failed_and_recovered_lines() -> None:
    text = _render(forfeit_events("forfeit", "RuntimeError: boom"))
    assert FLAG not in text
    assert "alice forfeited a challenge: malformed DUEL{<redacted>}" in text
    assert "arena check on alice's 'forfeit': rejected ✗ (forfeit) -5" in text
    assert "match failed during validate: RuntimeError: boom" in text
    assert "resume with: duel resume m" in text
    assert "resuming where the match stopped" in text


def test_draw() -> None:
    events = _state_events(
        Event(seq=1, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "build"}),
        Event(seq=2, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "validate"}),
        Event(seq=3, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "attack"}),
        Event(seq=4, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "score"}),
        Event(seq=5, type=ev.ROUND_SCORED, trace_id="t", data={"round": 1, "delta": {"alice": 0, "bob": 0}}),
        Event(seq=6, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "complete"}),
    )
    text = _render(events)
    assert "draw: alice 0  bob 0" in text
    assert "scoreboard after round 1: alice 0  bob 0" in text
    assert "spend: $0.0000 this match · $0.0031 of $33 cap" in text


def test_ledger_ceiling_cannot_exceed_key_limit() -> None:
    with pytest.raises(ValidationError, match="key_limit_usd"):
        BudgetLimits(lifetime_max_usd=60.0, key_limit_usd=50.0)


def test_labels_use_models_and_disambiguate() -> None:

    class A:
        def __init__(self, n: str, m: str) -> None:
            self.name, self.model = n, m

    assert labels([A("alice", "m1"), A("bob", "m2")]) == {"alice": "m1", "bob": "m2"}  # type: ignore[list-item]
    assert labels([A("alice", "m"), A("bob", "m")]) == {"alice": "alice", "bob": "bob"}  # type: ignore[list-item]


def test_labels_prefer_configured_names_when_models_match() -> None:
    """Live-lanes blocker: same model id must not force A/B over config names (fake-1-s2 / fake-1-s5)."""

    class A:
        def __init__(self, n: str, m: str) -> None:
            self.name, self.model = n, m

    assert labels([A("fake-1-s2", "fake-1"), A("fake-1-s5", "fake-1")]) == {  # type: ignore[list-item]
        "fake-1-s2": "fake-1-s2",
        "fake-1-s5": "fake-1-s5",
    }
