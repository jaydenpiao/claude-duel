"""Match caps are a terminal, void outcome (ADR 0014): CAPPED never resumes, replays, voids its series, unrated."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from claude_duel.app.budget import BudgetLimits
from claude_duel.app.container import build_event_log
from claude_duel.app.replay import replay
from claude_duel.app.series import play_series, verify_series
from claude_duel.app.session import play
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain import series as sr
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.events import fold
from claude_duel.domain.models import AgentSpec, Rules, ScoringRule, Stage
from claude_duel.domain.ratings import results
from claude_duel.domain.state_machine import TERMINAL
from claude_duel.observability import InMemoryMetrics


def test_match_cap_ends_capped_with_the_breaching_agent(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine(budget=BudgetLimits(match_max_calls=3)).run(new_state())
    assert state.stage is Stage.CAPPED
    hit = state.capped
    assert hit is not None
    assert (hit.limit, hit.spent, hit.cap) == ("match_calls", 3, 3)
    charged = [e for e in event_log.read("m1") if e.type == "call_charged"]
    assert len(charged) == 3
    assert hit.breaching_agent == charged[-1].data["agent"]  # whose call took usage to the cap
    assert (hit.stage, hit.round) == (Stage.BUILD, 1)


def test_resume_of_a_capped_match_makes_no_call_and_appends_nothing(  # type: ignore[no-untyped-def]
    make_engine, new_state, event_log, store
) -> None:
    make_engine(budget=BudgetLimits(match_max_calls=3)).run(new_state())
    before = event_log.read("m1")
    again = make_engine(budget=BudgetLimits(match_max_calls=500)).run(store.load("m1"))
    assert again.stage is Stage.CAPPED
    assert event_log.read("m1") == before


def test_replay_of_a_capped_log_reproduces_capped(make_engine, new_state, event_log, store) -> None:  # type: ignore[no-untyped-def]
    make_engine(budget=BudgetLimits(match_max_tokens=1)).run(new_state())
    folded = fold("m1", event_log.read("m1"))
    snap = store.load("m1")
    assert folded.stage is Stage.CAPPED
    assert folded.capped == snap.capped
    assert folded.scores == snap.scores
    assert replay(snap, event_log.read("m1")).consistent


def test_run_cap_still_fails_and_resumes(make_engine, new_state, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BudgetExceededError, match="run calls"):
        make_engine(budget=BudgetLimits(run_max_calls=3)).run(new_state())
    assert store.load("m1").stage is Stage.FAILED
    assert make_engine().run(store.load("m1")).stage is Stage.COMPLETE


@settings(max_examples=12, deadline=None)
@given(calls=st.integers(min_value=1, max_value=40))
def test_every_cap_terminates_and_terminal_resume_is_inert(tmp_path_factory, calls: int) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("cap")
    s = Settings(
        store_dir=root, sandbox="subprocess", rules=Rules(rounds=1), budget=BudgetLimits(match_max_calls=calls)
    )
    assert _stage(s, "p") in TERMINAL
    before = build_event_log(s).read("p")
    assert _stage(s, "p") in TERMINAL
    assert build_event_log(s).read("p") == before


def _stage(s: Settings, mid: str) -> Stage:
    return play(s, mid, InMemoryMetrics()).stage


AGENTS = (AgentSpec(name="alice", skill=5), AgentSpec(name="bob", skill=0))


def _series_settings(root: Path, on_cap: str = "void") -> Settings:
    rules = Rules(rounds=1, scoring=ScoringRule(on_cap=on_cap))  # type: ignore[arg-type]
    return Settings(
        store_dir=root, sandbox="subprocess", rules=rules, agents=AGENTS, budget=BudgetLimits(match_max_calls=3)
    )


def test_series_with_a_capped_game_ends_void_and_verifies(tmp_path: Path) -> None:
    s = _series_settings(tmp_path)
    state = play_series(s, "s", sr.SeriesRule(games=3), InMemoryMetrics())
    assert state.done()
    assert state.void()
    assert state.winner() is None
    assert [(g.game_no, g.void, g.winner) for g in state.games] == [(1, True, None)]  # no further games
    assert not (tmp_path / "s-g2.json").exists()
    again = play_series(s, "s", sr.SeriesRule(games=3), InMemoryMetrics())
    assert again.games == state.games  # the stall regression: done, nothing replayed with a new seed
    assert verify_series(s, "s").consistent


def test_partial_on_cap_counts_the_game_on_scored_rounds(tmp_path: Path) -> None:
    state = play_series(_series_settings(tmp_path, "partial"), "s", sr.SeriesRule(games=3), InMemoryMetrics())
    assert not state.void()
    assert all(not g.void for g in state.games)


def test_ratings_exclude_capped_matches_and_say_how_many(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _series_settings(tmp_path)
    capped = play(s, "c", InMemoryMetrics())
    assert capped.stage is Stage.CAPPED
    assert results([capped]) == []
    cfg = tmp_path / "duel.toml"
    cfg.write_text(
        f'store_dir = "{tmp_path}"\nsandbox = "subprocess"\n[budget]\nmatch_max_calls = 3\n', encoding="utf-8"
    )
    assert main(["--config", str(cfg), "ratings"]) == 0
    assert "1 capped match(es) excluded" in capsys.readouterr().err
