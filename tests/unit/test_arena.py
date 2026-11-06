"""Arena view: pure bar helpers, frame snapshots, and the live observer end to end."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from rich.console import Console
from rich.spinner import Spinner

from claude_duel import cli
from claude_duel.app.container import build_event_log
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain.models import Event, Rules
from claude_duel.observability import InMemoryMetrics
from claude_duel.presentation.arena import ArenaModel, ArenaView, Player, _lane, _short, frame, meter, sparkline, tug
from tests.helpers import FLAG, forfeit_events

SNAPSHOT = Path(__file__).parent.parent / "snapshots" / "arena_fake_match.txt"


def _console(width: int = 80) -> Console:
    return Console(file=io.StringIO(), width=width, force_terminal=False, no_color=True, record=True)


def _text(model: ArenaModel, width: int = 80) -> str:
    c = _console(width)
    c.print(frame(model, width))
    return c.export_text()


def test_bars() -> None:
    assert sparkline([]) == ""
    assert sparkline([0, 5, 10]) == "▁▄█"
    assert sparkline([3, 3]) == "▁▁"
    assert meter(5, 10, 10) == "█████░░░░░"
    assert meter(-4, 10, 2) == "░░░░"
    assert meter(3, 0, 4) == "░░░░"
    assert tug(0, 0, 5) == "━━◆━━"
    assert tug(10, 0, 5) == "◆━━━━"
    assert tug(0, 10, 5) == "━━━━◆"
    assert tug(-5, -5, 1) == "━━◆━━"


def test_full_match_snapshot(tmp_path: Path) -> None:
    console = _console()
    view = ArenaView(console, "snap", 33.0, lambda: 0.0031)
    play(Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(rounds=2)), "snap", InMemoryMetrics(), view)
    view.close()
    view.close()  # idempotent
    text = _text(view.model)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert text == SNAPSHOT.read_text(encoding="utf-8")
    assert "🏆 winner:" in text
    assert "\x1b[" not in console.export_text(styles=False)


@pytest.mark.parametrize("width", [1, 20, 40, 200])
def test_any_width_renders(width: int) -> None:
    m = ArenaModel(match_id="m", players=[Player("alice-with-a-long-name", "gpt-4o-mini", 9), Player("bob", "x", -3)])
    m.flash = "x" * 500
    lines = _text(m, width).splitlines()
    assert lines
    assert all(len(line) <= max(width, 20) for line in lines)


def test_forfeit_failure_and_recovery_never_show_flags() -> None:
    view = ArenaView(_console(), "m", 50.0, lambda: 0.0, pace=0.001)
    for e in forfeit_events(f"forfeit {FLAG}", f"RuntimeError: {FLAG}"):
        view(e)
    view.close()
    text = _text(view.model)
    assert FLAG not in text
    assert "alice forfeited a trap" in text
    assert "rejected ✗" in text
    assert "duel resume m" in text
    assert "resuming where the match stopped" in text


def test_spinner_frame_while_busy() -> None:
    m = ArenaModel(match_id="m", players=[Player("alice", "a")], now="attacking")
    c = _console()
    c.print(frame(m, 80, Spinner("dots")))
    assert "attacking" in c.export_text()


def test_cli_view_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    assert cli.main(["run", "--match-id", "a", "--view", "arena"]) == 0
    assert "🏆 winner:" in capsys.readouterr().out
    assert cli.main(["run", "--match-id", "b"]) == 0  # not a TTY: commentary
    assert "scoreboard after round" in capsys.readouterr().out


def test_attack_started_animates_and_old_logs_replay(tmp_path: Path) -> None:
    settings = Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(rounds=1))
    seen: list[str] = []
    view = ArenaView(_console(), "m", 50.0, lambda: 0.0)

    def spy(e: Event) -> None:
        view(e)
        if e.type == ev.ATTACK_STARTED:
            seen.append(view.model.now)

    state = play(settings, "m", InMemoryMetrics(), spy)
    view.close()
    assert seen
    assert "attacking" in seen[0]
    assert seen[0].endswith("... try 1/4")
    events = build_event_log(settings).read("m")
    # a log written before attack_started existed: same events without them, renumbered
    old = [e for e in events if e.type != ev.ATTACK_STARTED]
    old = [e.model_copy(update={"seq": i}) for i, e in enumerate(old)]
    folded = ev.initial_state("m", old[0])
    for e in old:
        ev.apply(folded, e)
    assert folded.scores == state.scores
    assert folded.attempts == state.attempts


def test_replay_rewatches_in_arena(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    assert cli.main(["run", "--match-id", "r", "--format", "json"]) == 0
    capsys.readouterr()
    assert cli.main(["replay", "r", "--view", "arena", "--pace", "0"]) == 0
    out = capsys.readouterr().out
    assert "🏆 winner:" in out
    assert "replay consistent=True" in out


def test_lane_shows_phase_actions_preview_and_thought_without_flags() -> None:
    p = Player("m1", "m1", key="alice", phase="attacking", preview=_short("Maze: DUEL{" + "ab" * 16 + "}"))
    for i in range(5):
        p.act(f"try {i}")
    p.thought = "route around the wall"
    text = "\n".join(t.plain for t in _lane(p, "cyan"))
    assert "m1 · attacking" in text
    assert "try 4" in text
    assert "try 1" not in text  # the tail keeps the last 3
    assert "💭 route around the wall" in text
    assert "DUEL{ab" not in text
