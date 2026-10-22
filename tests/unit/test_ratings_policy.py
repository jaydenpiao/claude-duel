"""Arch round 3 (majors): ratings rate handicapped agents separately, on raw scores, and skip bad logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_duel.cli import main
from claude_duel.domain.models import AgentSpec, Handicap, MatchState, Rules, SandboxLimits, Stage
from claude_duel.domain.ratings import entrant, raw_winner, results

PLAIN = AgentSpec(name="alice", skill=2)
CAPPED = PLAIN.model_copy(update={"name": "al2", "handicap": Handicap(score_offset=50, attempts_delta=-1)})
BOB = AgentSpec(name="bob", skill=3)


def _state(mid: str, a: AgentSpec, b: AgentSpec, scores: dict[str, int]) -> MatchState:
    return MatchState(
        match_id=mid, agents=(a, b), rules=Rules(), limits=SandboxLimits(), stage=Stage.COMPLETE, scores=scores
    )


def test_handicapped_agent_is_a_separate_entrant() -> None:
    assert entrant(CAPPED) != entrant(PLAIN)
    assert entrant(CAPPED).startswith(entrant(PLAIN) + "+hc(")
    rs = results([_state("m1", PLAIN, BOB, {"alice": 1, "bob": 0}), _state("m2", CAPPED, BOB, {"al2": 1, "bob": 0})])
    assert {r.a for r in rs} == {entrant(PLAIN), entrant(CAPPED)}


def test_offsets_never_decide_a_rated_result() -> None:
    state = _state("m1", CAPPED, BOB, {"al2": 10, "bob": 20})
    assert state.final_scores()["al2"] > state.final_scores()["bob"]  # the offset wins the match result...
    assert raw_winner(state) == "bob"  # ...but not the rating
    (r,) = results([state])
    assert r.score_a == 0.0


def test_unreadable_logs_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "duel.toml"
    cfg.write_text(f'sandbox = "subprocess"\nstore_dir = "{tmp_path / "s"}"\n[rules]\nrounds = 1\n')
    assert main(["--config", str(cfg), "run", "--match-id", "ok1", "--format", "json"]) == 0
    (tmp_path / "s" / "bad1.events.jsonl").write_text('{"not": "a chained event"}\n')
    capsys.readouterr()
    assert main(["--config", str(cfg), "ratings", "--format", "json", "--boot", "0"]) == 0
    out = capsys.readouterr()
    assert len(json.loads(out.out)) == 2
    assert "1 log(s) skipped: bad1" in out.err
