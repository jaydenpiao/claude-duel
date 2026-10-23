"""Arch round 3 #15: tournament_created pins each entrant's model, so a later config change can't re-identify it."""

from __future__ import annotations

from pathlib import Path

from claude_duel.app.budget import FAKE_MODEL
from claude_duel.app.container import build_tournament_log, pin_entrants
from claude_duel.app.tournament import play_tournament
from claude_duel.config import Settings
from claude_duel.domain import tournament as tr
from claude_duel.domain.models import AgentSpec, Rules
from claude_duel.observability import InMemoryMetrics


def test_pin_entrants_resolves_the_default_model() -> None:
    entrants = (AgentSpec(name="a", provider="openai"), AgentSpec(name="b", provider="openai", model="m2"))
    pinned = pin_entrants(Settings(openai_model="m1"), entrants)
    assert [e.model for e in pinned] == ["m1", "m2"]


def test_created_event_records_pinned_entrants(tmp_path: Path) -> None:
    settings = Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(rounds=1, challenges_per_round=1))
    rule = tr.TournamentRule(entrants=tuple(AgentSpec(name=f"e{i}", skill=i) for i in range(3)))
    play_tournament(settings, "t", rule, InMemoryMetrics())
    created = build_tournament_log(settings).read("t")[0]
    assert [e["model"] for e in created.data["rule"]["entrants"]] == [FAKE_MODEL] * 3
