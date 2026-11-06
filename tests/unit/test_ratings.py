"""Ratings (#39): Elo golden values, Bradley-Terry order independence and monotonicity, `duel ratings`."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.cli import main
from claude_duel.domain.models import AgentSpec, Event, MatchState, Rules, SandboxLimits, Stage
from claude_duel.domain.ratings import ELO_START, Result, bradley_terry, elo, entrant, ratings, results


def test_elo_golden_three_games() -> None:
    rs = [Result("m1", "a", "b", 1.0), Result("m2", "a", "c", 0.5), Result("m3", "b", "c", 0.0)]
    r = elo(rs)
    # m1: a 1516, b 1484. m2: E(a)=1/(1+10^(-16/400))=0.52302 -> a 1515.26, c 1500.74.
    # m3: E(b)=1/(1+10^((1500.74-1484)/400))=0.47593 -> b 1468.77, c 1515.97.
    assert r["a"] == pytest.approx(1515.264, abs=0.01)
    assert r["b"] == pytest.approx(1468.770, abs=0.01)
    assert r["c"] == pytest.approx(1515.966, abs=0.01)
    assert sum(r.values()) == pytest.approx(3 * ELO_START)


def _round_robin(n: int, seed: int = 1) -> list[Result]:
    """Skills 1 < 2 < 3: the stronger side wins with probability s_a / (s_a + s_b) (a BT world)."""
    rng = random.Random(seed)
    out = []
    pairs = [(1, 2), (1, 3), (2, 3)]
    for i in range(n):
        for a, b in pairs:
            out.append(Result(f"{a}{b}-{i:03d}", f"s{a}", f"s{b}", 1.0 if rng.random() < a / (a + b) else 0.0))
    return out


def test_bradley_terry_is_order_independent_and_monotone() -> None:
    rs = _round_robin(30)
    shuffled = rs[:]
    random.Random(7).shuffle(shuffled)
    assert bradley_terry(rs) == pytest.approx(bradley_terry(shuffled))
    table = ratings(shuffled, boot=50)
    assert [r.entrant for r in table] == ["s3", "s2", "s1"]
    assert all(r.lo <= r.bt <= r.hi for r in table)
    assert all(r.games == 60 for r in table)
    assert table == ratings(rs, boot=50)
    assert bradley_terry([]) == {}


def _state(mid: str, a: AgentSpec, b: AgentSpec, scores: dict[str, int], stage: Stage = Stage.COMPLETE) -> MatchState:
    return MatchState(match_id=mid, agents=(a, b), rules=Rules(), limits=SandboxLimits(), stage=stage, scores=scores)


def test_results_skip_unfinished_and_self_play() -> None:
    a, b = AgentSpec(name="alice", skill=2), AgentSpec(name="bob", skill=3)
    real = AgentSpec(name="carol", provider="openai", model="gpt-4o-mini")
    states = [
        _state("m2", a, b, {"alice": 1, "bob": 0}),
        _state("m1", b, real, {"bob": 2, "carol": 2}),
        _state("m3", a, a.model_copy(update={"name": "al2"}), {"alice": 5}),
        _state("m4", a, b, {"alice": 1}, Stage.BUILD),
    ]
    rs = results(states)
    assert [(r.match_id, r.score_a) for r in rs] == [("m1", 0.5), ("m2", 1.0)]
    assert entrant(real) == "openai:gpt-4o-mini"
    assert entrant(a) == "fake:fake@skill2"


def test_cli_ratings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "duel.toml"
    cfg.write_text('sandbox = "subprocess"\n[rules]\nrounds = 1\n')
    assert main(["--config", str(cfg), "run", "--match-id", "r1", "--format", "json"]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfg), "ratings", "--format", "json", "--boot", "5"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert {r["games"] for r in rows} == {1}
    assert main(["--config", str(cfg), "ratings", "--boot", "0"]) == 0
    assert "95% ci" in capsys.readouterr().out


def test_elo_folds_in_recorded_order_golden() -> None:  # #137: three matches created in a known order
    rs = [Result("zzz", "a", "b", 1.0, 0), Result("mmm", "b", "c", 1.0, 1), Result("aaa", "a", "c", 0.0, 2)]
    want = {"a": 1500.0, "b": 1500.0, "c": 1500.0}
    for x in rs:  # the hand fold, in creation order
        e = 1 / (1 + 10 ** ((want[x.b] - want[x.a]) / 400))
        want[x.a] += 32 * (x.score_a - e)
        want[x.b] -= 32 * (x.score_a - e)
    for _ in range(5):
        random.shuffle(rs)  # shuffling log/file order leaves Elo unchanged
        assert elo(rs) == pytest.approx(want)
    assert elo(rs) != pytest.approx(elo([Result(r.match_id, r.a, r.b, r.score_a) for r in rs]))


def test_series_games_sort_numerically_with_decider_after_its_game() -> None:
    ids = [f"s-g{i}" for i in range(10, 0, -1)] + ["s-g2-sd"]
    got = [r.match_id for r in sorted((Result(i, "a", "b", 1.0) for i in ids), key=lambda r: r.order)]
    assert got[:4] == ["s-g1", "s-g2", "s-g2-sd", "s-g3"]
    assert got[-1] == "s-g10"


def test_store_assigns_created_seq(tmp_path: Path) -> None:
    log = FileEventLog(tmp_path)
    for m in ("m-b", "m-a"):
        log.append(m, Event(seq=0, type="match_created", trace_id="t", data={}))
        log.append(m, Event(seq=1, type="x", trace_id="t", data={}))
    assert [log.read(m)[0].data["created_seq"] for m in ("m-b", "m-a")] == [0, 1]
    assert "created_seq" not in log.read("m-a")[1].data


def test_bootstrap_ci_ignores_resamples_without_the_entrant() -> None:
    """#138: a rarely seen strong entrant's CI is not dragged to 1500 by resamples that miss it."""
    rs = [*_round_robin(20), Result("zz-1", "solo", "s1", 1.0), Result("zz-2", "solo", "s2", 1.0)]
    solo = next(r for r in ratings(rs, boot=200) if r.entrant == "solo")
    assert solo.bt > ELO_START
    assert solo.lo > ELO_START  # with the old 1500 fill-in, ~13% of resamples sat at exactly 1500
