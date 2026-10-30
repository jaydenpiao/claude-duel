"""Shared challenge pool with CTFd-style decay (#39): opt-in, after the pairings, resumable, replayable."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_duel.app.container import build_event_log, build_store, build_tournament_log
from claude_duel.app.framings import framing_text
from claude_duel.app.replay import replay
from claude_duel.app.tournament import play_tournament, pool_challenges
from claude_duel.config import Settings
from claude_duel.domain import tournament as tr
from claude_duel.domain.models import AgentSpec, FramingRule, Rules
from claude_duel.observability import InMemoryMetrics


def test_ctfd_formulas() -> None:
    lin = tr.DecayRule(initial=100, minimum=10, decay=20)
    assert [tr.decay_value(lin, n) for n in range(1, 7)] == [100, 80, 60, 40, 20, 10]
    log = tr.DecayRule(function="logarithmic", initial=100, minimum=10, decay=3)
    values = [tr.decay_value(log, n) for n in range(1, 6)]
    assert values == [100, 90, 60, 10, 10]  # ceil(-90/9 * n^2 + 100), floored at minimum
    assert tr.decay_value(lin, 0) == 100
    points = tr.pool_points(lin, {"c1": ["a", "b"], "c2": ["a"], "c3": []}, ["a", "b", "c"])
    assert points == {"a": 180, "b": 80, "c": 0}


def test_pool_is_opt_in_and_reserved() -> None:
    entrants = tuple(AgentSpec(name=f"e{i}") for i in range(3))
    assert not tr.TournamentRule(entrants=entrants).pool
    with pytest.raises(ValueError, match="reserved"):
        tr.TournamentRule(entrants=(*entrants[:2], AgentSpec(name=tr.POOL)))


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1, challenges_per_round=1))


def test_pool_tournament(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    entrants = (AgentSpec(name="weak", skill=0), AgentSpec(name="mid", skill=3), AgentSpec(name="top", skill=5))
    rule = tr.TournamentRule(entrants=entrants, pool=True)
    state = play_tournament(settings, "p1", rule, InMemoryMetrics())
    assert state.pool_points is not None
    pool = pool_challenges(settings, state)
    assert pool
    store, log = build_store(settings), build_event_log(settings)
    solvers: dict[str, list[str]] = {c.id: [] for c in pool}
    for e in entrants:
        match = store.load(f"p1-pool-{e.name}")
        assert {k.split(":")[0] for k in match.attacks} == {c.id for c in pool if c.author != e.name}
        assert replay(match, log.read(match.match_id)).consistent
        solvers.update(
            {k.split(":")[0]: [*solvers[k.split(":")[0]], e.name] for k, a in match.attacks.items() if a.solved}
        )
    assert state.pool_points == tr.pool_points(rule.decay, solvers, [e.name for e in entrants])
    again = play_tournament(settings, "p1", rule, InMemoryMetrics())  # resume: nothing re-attacked or re-scored
    assert again.pool_points == state.pool_points
    assert tr.fold("p1", build_tournament_log(settings).read("p1")).pool_points == state.pool_points
    assert len(build_tournament_log(settings).read("p1")) == len(state.pairings) + 2


def test_tie_on_points_falls_to_pool_points() -> None:
    entrants = tuple(AgentSpec(name=f"e{i}") for i in range(3))
    st = tr.TournamentState(tournament_id="t", seed="s", rule=tr.TournamentRule(entrants=entrants))
    st.pool_points = {"e0": 0, "e1": 0, "e2": 50}
    assert st.table()[0][0] == "e2"


def test_pool_matches_pin_framing(tmp_path: Path) -> None:
    """Pool match creation goes through open_match, so framing_text is pinned."""
    rules = Rules(rounds=1, challenges_per_round=1, framing=FramingRule(kind="notice"))
    settings = _settings(tmp_path).model_copy(update={"rules": rules})
    entrants = (AgentSpec(name="a", skill=5), AgentSpec(name="b", skill=5), AgentSpec(name="c", skill=5))
    state = play_tournament(settings, "pf", tr.TournamentRule(entrants=entrants, pool=True), InMemoryMetrics())
    store = build_store(settings)
    expected = framing_text(settings.rules.framing)
    assert expected  # notice framing is non-empty
    for e in entrants:
        match = store.load(f"pf-pool-{e.name}")
        assert match.framing_text == expected
    assert state.pool_points is not None
