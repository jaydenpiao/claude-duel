"""open_match shared by session.play and tournament pool."""

from __future__ import annotations

from pathlib import Path

from claude_duel.app.container import build_store, pin_agents
from claude_duel.app.framings import framing_text
from claude_duel.app.session import open_match
from claude_duel.config import Settings
from claude_duel.domain.models import AgentSpec, FramingRule, Rules


def test_open_match_pins_framing_and_is_idempotent(tmp_path: Path) -> None:
    settings = Settings(
        store_dir=tmp_path,
        rules=Rules(rounds=1, framing=FramingRule(kind="notice")),
        agents=(AgentSpec(name="a"), AgentSpec(name="b")),
    )
    store = build_store(settings)
    first = open_match(store, settings, "m1")
    assert first.framing_text == framing_text(settings.rules.framing)
    assert first.agents == pin_agents(settings)
    again = open_match(store, settings, "m1", agents=(AgentSpec(name="x"), AgentSpec(name="y")), seed="nope")
    assert again.framing_text == first.framing_text
    assert again.seed == first.seed
    assert again.agents == first.agents  # resume keeps saved agents
