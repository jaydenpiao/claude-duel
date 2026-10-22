"""Tournaments (#39): round-robin schedule, Swiss pairing properties, deterministic resumable play."""

from __future__ import annotations

import json
import time
from itertools import combinations
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.app.container import build_tournament_log
from claude_duel.app.tournament import play_tournament
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain import tournament as tr
from claude_duel.domain.errors import BudgetExceededError, EventLogCorruptError
from claude_duel.domain.models import AgentSpec, Event, Rules
from claude_duel.observability import InMemoryMetrics


def _entrants(n: int) -> tuple[AgentSpec, ...]:
    return tuple(AgentSpec(name=f"e{i}", skill=i % 6) for i in range(n))


@pytest.mark.parametrize("n", [3, 4, 5, 6])
def test_round_robin_meets_everyone_once(n: int) -> None:
    names = [f"e{i}" for i in range(n)]
    rounds = tr.round_robin(names)
    games = [frozenset(p) for r in rounds for p in r if tr.BYE not in p]
    assert sorted(map(sorted, games)) == sorted(map(sorted, map(frozenset, combinations(names, 2))))
    byes = [p[0] for r in rounds for p in r if p[1] == tr.BYE]
    assert sorted(byes) == (sorted(names) if n % 2 else [])
    assert all(
        len({x for p in r for x in p if x != tr.BYE}) == len([x for p in r for x in p if x != tr.BYE]) for r in rounds
    )


@given(n=st.integers(3, 10), seed=st.text(min_size=1, max_size=4), data=st.data())
def test_swiss_avoids_rematches_while_possible(n: int, seed: str, data: st.DataObject) -> None:
    names = [f"e{i}" for i in range(n)]
    played = set(data.draw(st.sets(st.sampled_from([frozenset(p) for p in combinations(names, 2)]), max_size=n)))
    table = sorted(
        ((x, float(data.draw(st.integers(0, 3)))) for x in names), key=lambda kv: (-kv[1], tr.tiebreak(seed, kv[0]))
    )
    pairs = tr.swiss_round(table, played, set())
    assert pairs == tr.swiss_round(table, played, set())  # deterministic
    seen = [x for p in pairs for x in p if x != tr.BYE]
    assert sorted(seen) == sorted(names)
    real = [frozenset(p) for p in pairs if tr.BYE not in p]
    if tr._pair([x for x in seen if not (n % 2 and (x, tr.BYE) in pairs)], played) is not None:
        assert not set(real) & played


def test_rule_validation_and_kinds() -> None:
    assert tr.TournamentRule(entrants=_entrants(6)).kind() == "round_robin"
    assert tr.TournamentRule(entrants=_entrants(7)).rounds() == 3
    assert tr.TournamentRule(entrants=_entrants(5)).rounds() == 5
    assert tr.TournamentRule(entrants=_entrants(4)).rounds() == 3
    with pytest.raises(ValueError, match="unique"):
        tr.TournamentRule(entrants=(*_entrants(3), AgentSpec(name="e0")))
    with pytest.raises(EventLogCorruptError):
        tr.fold("t", [])
    rule = tr.TournamentRule(entrants=_entrants(3))
    created = Event(
        seq=0, type=tr.TOURNAMENT_CREATED, trace_id="t", data={"seed": "s", "rule": rule.model_dump(mode="json")}
    )
    with pytest.raises(EventLogCorruptError, match="unknown"):
        tr.fold("t", [created, Event(seq=1, type="x", trace_id="t", data={})])
    wrong = {"round": 3, "a": "e0", "b": "e1", "winner": None}
    with pytest.raises(EventLogCorruptError, match="out of order"):
        tr.fold("t", [created, Event(seq=1, type=tr.PAIRING_FINISHED, trace_id="t", data=wrong)])


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1, challenges_per_round=1))


def test_swiss_tournament_plays_and_resumes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    rule = tr.TournamentRule(entrants=_entrants(4), format="swiss", swiss_rounds=2)
    state = play_tournament(settings, "t1", rule, InMemoryMetrics())
    assert state.next_pair() is None
    assert len(state.pairings) == 4
    assert len({frozenset((p.a, p.b)) for p in state.pairings}) == 4  # no rematch
    assert sum(state.standings().values()) == 4
    again = play_tournament(settings, "t1", rule, InMemoryMetrics())
    assert again.pairings == state.pairings
    assert tr.fold("t1", build_tournament_log(settings).read("t1")).table() == state.table()


def test_tournament_cap(tmp_path: Path) -> None:
    rule = tr.TournamentRule(entrants=_entrants(3), tournament_max_usd=1e-12)
    state = play_tournament(_settings(tmp_path), "t2", rule, InMemoryMetrics())  # fake games are free
    assert state.next_pair() is None
    paid = state.pairings[0].model_copy(update={"usd": 1.0})  # replay a log whose first pairing cost $1
    log = build_tournament_log(_settings(tmp_path / "x"))
    created = build_tournament_log(_settings(tmp_path)).read("t2")[0]
    log.append("t3", created)
    log.append("t3", Event(seq=1, type=tr.PAIRING_FINISHED, trace_id="t3", data=paid.model_dump()))
    with pytest.raises(BudgetExceededError, match="tournament cost"):
        play_tournament(_settings(tmp_path / "x"), "t3", rule, InMemoryMetrics())


def test_cli_tournament(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "duel.toml"
    body = 'sandbox = "subprocess"\n[rules]\nrounds = 1\nchallenges_per_round = 1\n'
    cfg.write_text(body)
    assert main(["--config", str(cfg), "tournament"]) == 2
    assert "no [tournament]" in capsys.readouterr().err
    cfg.write_text(body + "".join(f'[[tournament.entrants]]\nname = "e{i}"\nskill = {i}\n' for i in range(3)))
    assert main(["--config", str(cfg), "tournament", "--tournament-id", "c1", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["finished"]
    assert out["format"] == "round_robin"
    assert len(out["pairings"]) == 6  # 3 games + 3 byes
    assert main(["--config", str(cfg), "tournament", "--tournament-id", "c1"]) == 0
    assert "1. " in capsys.readouterr().out


def test_swiss_32_entrants_20_rounds_plays_and_folds_fast() -> None:  # issue #135
    rule = tr.TournamentRule(entrants=_entrants(32), format="swiss", swiss_rounds=20)
    state = tr.TournamentState(tournament_id="t", seed="s", rule=rule)
    start = time.perf_counter()
    events = [
        Event(seq=0, type=tr.TOURNAMENT_CREATED, trace_id="t", data={"seed": "s", "rule": rule.model_dump(mode="json")})
    ]
    while (nxt := state.next_pair()) is not None:
        r, (a, b) = nxt
        p = tr.Pairing(round=r, a=a, b=b, winner=None if b == tr.BYE else min(a, b))
        state.pairings.append(p)
        events.append(Event(seq=len(events), type=tr.PAIRING_FINISHED, trace_id="t", data=p.model_dump()))
    assert len(state.pairings) == 32 * 20 // 2
    assert tr.fold("t", events).pairings == state.pairings
    assert time.perf_counter() - start < 10


def test_unpairable_remainder_is_memoised() -> None:  # an odd clique that may only meet itself: no pairing exists
    order = [f"p{i}" for i in range(32)]
    clique, others = order[:15], order[15:]
    played = {frozenset((a, b)) for a in clique for b in others}
    start = time.perf_counter()
    assert tr._pair(order, played) is None
    assert time.perf_counter() - start < 2
