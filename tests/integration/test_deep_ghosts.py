"""Deep layer phase 1: Ghost Codex replay (design-deep-layer.md section 9, tests 1-6 and 9)."""

from __future__ import annotations

import random

from hypothesis import given
from hypothesis import strategies as st

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain import ghosts
from claude_duel.domain.models import Challenge, DeepRule, MatchState, Stage
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import solve_award
from claude_duel.ports import Completion, CompletionRequest


class Recording:
    name = "recording"

    def __init__(self, skill: int) -> None:
        self.inner, self.calls = FakeProvider(skill=skill), []  # type: ignore[var-annotated]

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls.append((request.purpose, request.prompt))
        return self.inner.complete(request)


def _deep(new_state, match_id: str = "d1", **kw) -> MatchState:  # type: ignore[no-untyped-def]
    state: MatchState = new_state(match_id)
    state.rules = preset("ledger_deep").model_copy(update={"rounds": 2, **kw})
    state.seed = "seed-1"
    return state


def _runs(event_log, mid: str = "d1") -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    return [e.data for e in event_log.read(mid) if e.type == ev.GHOST_RUN]


def test_ghost_runs_are_deterministic_and_replay(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    a = make_engine().run(_deep(new_state, "d1"))
    b = make_engine().run(_deep(new_state, "d2"))
    assert a.stage is Stage.COMPLETE
    assert a.scores == b.scores

    def shape(mid: str) -> list[tuple[bool, str]]:  # challenge ids embed the match id; outcomes must not differ
        return sorted((r["hit"], r["stdout_sha"]) for r in _runs(event_log, mid))

    assert shape("d1") == shape("d2")
    assert any(r["hit"] for r in _runs(event_log))
    assert replay(a, event_log.read("d1")).consistent


def _ch(cid: str, author: str, rnd: int = 2) -> Challenge:
    return Challenge(id=cid, author=author, round=rnd, spec=None)


def test_ghost_order_is_pure_in_seed_round_and_ids() -> None:
    gs = [ghosts.Ghost(id=f"g{i}:bob", attacker="bob", round=1, payload="x") for i in range(4)]
    cs = [_ch(f"c{i}", "alice") for i in range(3)] + [_ch("own", "bob")]
    plan = [(g.id, c.id) for g, c in ghosts.ghost_schedule("s", 2, gs, cs, 50)]
    for _ in range(5):
        random.shuffle(gs), random.shuffle(cs)  # type: ignore[func-returns-value]
        assert [(g.id, c.id) for g, c in ghosts.ghost_schedule("s", 2, gs, cs, 50)] == plan
    assert len(plan) == 12
    assert all(c != "own" for _, c in plan)
    assert [(g.id, c.id) for g, c in ghosts.ghost_schedule("t", 2, gs, cs, 50)] != plan
    assert [(g.id, c.id) for g, c in ghosts.ghost_schedule("s", 2, gs, cs, 5)] == plan[:5]


def test_ghost_hits_an_identical_challenge_and_misses_a_disjoint_one(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_deep(new_state))
    src = {c.id: c.spec.source for c in state.challenges.values() if c.spec}
    runs = _runs(event_log)
    same = [r for r in runs if src[r["target"]] == src[r["ghost_id"].split(":")[0]]]
    assert same
    assert all(r["hit"] for r in same)
    assert any(not r["hit"] for r in runs)


@given(st.integers(min_value=1, max_value=10_000))
def test_ghost_award_is_a_quarter_at_least_one(full: int) -> None:
    assert 1 <= ghosts.ghost_award(full) <= max(1, full // 4)


def test_ghost_capture_pays_the_quarter_and_defense_still_beats_it(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_deep(new_state))
    rule = state.rules.scoring
    for atk in (a for a in state.attacks.values() if a.ghost):
        spec = state.challenges[atk.challenge_id].spec
        assert spec is not None
        full = solve_award(spec, 2, 1, rule)
        assert ghosts.ghost_award(full) <= rule.defense_points * spec.difficulty * 2  # ADR 0006


def test_ghost_runs_are_capped_and_make_no_model_calls(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Recording(2), "bob": Recording(5)}
    state = make_engine(providers=provs).run(_deep(new_state, deep=DeepRule(ghosts=True, max_ghost_runs=2)))
    assert 0 < len(_runs(event_log)) <= 2
    live = sum(a.attempts for a in state.attacks.values())
    attack_calls = sum(p == "attack" for prov in provs.values() for p, _ in prov.calls)
    assert attack_calls == live
    assert not any(p == "ghost" for prov in provs.values() for p, _ in prov.calls)


def test_ghost_capture_skips_the_live_attack_and_refunds_the_stake(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Recording(2), "bob": Recording(5)}
    state = make_engine(providers=provs).run(_deep(new_state))
    hit = [a for a in state.attacks.values() if a.ghost]
    assert hit
    assert all(a.solved and a.attempts == 0 and a.tokens_used == 0 for a in hit)
    for a in hit:
        title = state.challenges[a.challenge_id].spec.title  # type: ignore[union-attr]
        assert not any(p == "attack" and title in t and "Round 2" in t for p, t in provs[a.attacker].calls)
    assert state.usage_by_agent  # charged usage comes only from live calls; ghosts charge nothing


def test_ledger_preset_has_no_ghosts(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    assert preset("ledger").deep is None
    assert preset("ledger_deep").deep == DeepRule(ghosts=True)
    state: MatchState = new_state("l1")
    state.rules = preset("ledger").model_copy(update={"rounds": 2})
    make_engine().run(state)
    assert not _runs(event_log, "l1")
    assert not any(a.ghost for a in state.attacks.values())
