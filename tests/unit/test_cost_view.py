"""Tokens and cost per agent (#146): the usage split folds from call_charged and agent_cost divides it right."""

from __future__ import annotations

from pathlib import Path

from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain.models import MatchState, Rules, Usage
from claude_duel.observability import InMemoryMetrics
from claude_duel.presentation.common import agent_cost


def _played(tmp_path: Path) -> MatchState:
    settings = Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(rounds=2))
    state = play(settings, "cost", InMemoryMetrics())
    assert isinstance(state, MatchState)
    return state


def test_usage_split_sums_to_tokens(tmp_path: Path) -> None:
    s = _played(tmp_path)
    assert s.usage.input_tokens + s.usage.output_tokens == s.usage.tokens
    by = s.usage_by_agent.values()
    assert sum(u.input_tokens for u in by) == s.usage.input_tokens
    assert sum(u.output_tokens for u in by) == s.usage.output_tokens


def test_agent_cost_per_capture_and_defense(tmp_path: Path) -> None:
    s = _played(tmp_path)
    s.usage_by_agent["alice"] = Usage(usd=0.01, input_tokens=1000, output_tokens=200, reasoning_tokens=50)
    a = {k: x for k, x in s.attacks.items() if x.attacker == "alice"}
    first, *rest = a
    s.attacks[first] = a[first].model_copy(update={"solved": True, "usd_micros": 4000})
    for k in rest:
        s.attacks[k] = a[k].model_copy(update={"solved": False, "usd_micros": 0})
    held = [x for x in s.attacks.values() if not x.solved and s.challenges[x.challenge_id].author == "alice"]
    line = agent_cost(s, "alice")
    assert line.startswith("in 1,000 · out 200 (think 50) · $0.0100 · $0.0040/capture")
    assert line.endswith(f"${0.006 / len(held):.4f}/defense" if held else "—/defense")


def test_agent_cost_without_calls_is_dashes(tmp_path: Path) -> None:
    s = _played(tmp_path)
    s.usage_by_agent.clear()
    s.attacks.clear()
    assert agent_cost(s, "alice") == "in 0 · out 0 (think 0) · $0.0000 · —/capture · —/defense"
