"""The launch config pins what docs/runs/launch-prereg.md says (#148), and stays symmetric."""

from __future__ import annotations

from pathlib import Path

from claude_duel.config import load_settings

ROOT = Path(__file__).parent.parent.parent
CONFIG = ROOT / "docs" / "runs" / "launch.toml"


def test_launch_config_matches_prereg() -> None:
    s = load_settings(CONFIG, env={})
    r = s.rules
    assert (r.mode, r.rounds, r.challenges_per_round, r.max_attempts) == ("checked", 3, 1, 3)
    assert (r.max_output_tokens, r.reasoning_effort, r.variety) == (8000, "low", True)
    assert r.framing.kind == "neutral"
    assert [a.name for a in s.agents] == ["a", "b"]  # neutral labels, never vendor names
    assert all(a.handicap == s.agents[0].handicap for a in s.agents)
    doc = (ROOT / "docs" / "runs" / "launch-prereg.md").read_text(encoding="utf-8")
    assert "`max_output_tokens = 8000`" in doc
    assert '`reasoning_effort = "low"`' in doc
