"""$0 no-fluke harness smoke: a much stronger scripted bot wins its duplicate matches; equal bots show no seat bias."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.adapters.providers.fake import TEMPLATES
from claude_duel.adapters.providers.scripted import ScriptedProvider
from claude_duel.app.harness import measure
from claude_duel.cli import main
from claude_duel.domain.duplicate import DuplicateRule
from claude_duel.ports import CompletionRequest

RULE = DuplicateRule(boards_min=4, boards_max=4)


def _attack(prompt: str) -> CompletionRequest:
    return CompletionRequest(system="s", prompt=prompt, purpose="attack", max_tokens=100)


def test_scripted_bot_hits_at_its_rate_and_repeats() -> None:
    prompt = f"source:\n{TEMPLATES[2].source}"
    first = ScriptedProvider(30, "x").complete(_attack(prompt)).text
    bot = ScriptedProvider(30, "x")
    texts = [bot.complete(_attack(prompt)).text for _ in range(400)]
    hits = sum(TEMPLATES[2].payload in t for t in texts)
    assert 90 <= hits <= 150
    assert texts[0] == first  # same seed, same draws
    assert all(TEMPLATES[2].payload not in ScriptedProvider(0, "y").complete(_attack(prompt)).text for _ in range(20))


def test_strong_bot_wins_and_equal_bots_split_seats(tmp_path: Path) -> None:
    strong = measure(tmp_path / "a", 90, 10, matches=2, rule=DuplicateRule(boards_min=4, boards_max=6))
    assert strong.first == 2
    even = measure(tmp_path / "b", 50, 50, matches=1, rule=RULE, seed="e")
    assert even.first_seat_share == pytest.approx(0.5, abs=0.25)


def test_cli_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    assert (
        main(["harness", "--pct-a", "90", "--pct-b", "10", "--matches", "1", "--boards-min", "2", "--boards-max", "2"])
        == 0
    )
    assert "90% vs 10% over 1 matches" in capsys.readouterr().out
    assert main(["harness", "--boards-min", "5", "--boards-max", "2"]) == 2
