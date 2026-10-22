"""Replay card: pure SVG snapshot, redaction/escaping, and `duel card` end to end."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_duel import cli
from claude_duel.presentation.card import CardData, card_svg
from tests.helpers import FLAG

SNAPSHOT = Path(__file__).parent.parent / "snapshots" / "card.svg"


def _data(**kw: object) -> CardData:
    base: dict[str, object] = {
        "match_id": "snap",
        "players": [("alice", "gpt-4.1-nano", 96), ("bob", "gpt-4.1-mini", -5)],
        "rounds": "2/2 rounds · complete",
        "headline": "winner: alice · alice 96  bob -5",
        "spend": "$0.0031 · 30 model calls · 9696 tokens",
        "verified": True,
        "digest": "0123456789abcdef",
    }
    base.update(kw)
    return CardData(**base)  # type: ignore[arg-type]


def test_card_snapshot() -> None:
    svg = card_svg(_data())
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(svg, encoding="utf-8")
    assert svg == SNAPSHOT.read_text(encoding="utf-8")


def test_card_redacts_and_escapes() -> None:
    svg = card_svg(_data(headline=f"<b>{FLAG}</b> sk-abcdefgh12345678", verified=False))
    assert FLAG not in svg
    assert "sk-abcdefgh" not in svg
    assert "<b>" not in svg
    assert "replay MISMATCH" in svg


def test_cli_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    monkeypatch.setenv("DUEL_ROUNDS", "1")
    assert cli.main(["run", "--match-id", "m", "--format", "json"]) == 0
    assert cli.main(["card", "m"]) == 0
    svg = (tmp_path / "m-card.svg").read_text(encoding="utf-8")
    assert "replay verified ✓" in svg
    assert "card written: m-card.svg" in capsys.readouterr().out
    assert cli.main(["card", "m", "--out", str(tmp_path / "x.svg")]) == 0
    assert (tmp_path / "x.svg").exists()
