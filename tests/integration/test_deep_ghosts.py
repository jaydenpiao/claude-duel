"""Deep layer phase 1: Ghost Codex replay (design-deep-layer.md section 9, tests 1-6 and 9)."""

from __future__ import annotations

import hashlib
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.app.ghost_verify import verify_ghosts
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain import ghosts, terrain
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.models import (
    CAPTURED,
    Attack,
    AttackAttempt,
    Challenge,
    DeepRule,
    MatchState,
    ScoringRule,
    Stage,
    Validation,
)
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import SCORERS, RoundFacts, capture_points, defense_award, solve_award
from claude_duel.domain.stakes import settle_stakes
from claude_duel.ports import Completion, CompletionRequest
from tests.helpers import any_source
from tests.integration.test_ledger_match import _log_rows
from tests.unit.test_domain import spec


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

    rows, other = _log_rows(event_log, "d1"), _log_rows(event_log, "d2")
    # whole log, in order, minus per-match ids and latency
    assert next(((x, y) for x, y in zip(rows, other, strict=True) if x != y), None) is None
    assert (
        hashlib.sha256("\n".join(rows).encode()).hexdigest()
        == hashlib.sha256("\n".join(_log_rows(event_log, "d2")).encode()).hexdigest()
    )
    assert any(r["hit"] for r in _runs(event_log))
    ra, rb = replay(a, event_log.read("d1")), replay(b, event_log.read("d2"))
    assert ra.consistent
    assert rb.consistent
    assert ra.recomputed == rb.recomputed == a.scores


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
        assert ghosts.ghost_award(full) <= defense_award(spec, 2, rule)  # ADR 0006; the property test is shared


def test_ghost_runs_are_capped_and_make_no_model_calls(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Recording(2), "bob": Recording(5)}
    state = make_engine(providers=provs).run(_deep(new_state, deep=DeepRule(ghosts=True, max_ghost_runs=2)))
    assert 0 < len(_runs(event_log)) <= 2
    live = sum(a.attempts for a in state.attacks.values())
    attack_calls = sum(p == "attack" for prov in provs.values() for p, _ in prov.calls)
    assert attack_calls == live
    assert not any(p == "ghost" for prov in provs.values() for p, _ in prov.calls)


def test_ghost_capture_skips_the_live_attack_and_refunds_the_stake(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Recording(2), "bob": Recording(5)}
    state = make_engine(providers=provs).run(_deep(new_state))
    hit = [a for a in state.attacks.values() if a.ghost]
    assert hit
    assert all(a.solved and a.attempts == 0 and a.tokens_used == 0 for a in hit)
    for a in hit:
        title = state.challenges[a.challenge_id].spec.title  # type: ignore[union-attr]
        assert not any(p == "attack" and title in t and "Round 2" in t for p, t in provs[a.attacker].calls)
        assert f"{a.challenge_id}:{a.attacker}" not in state.attempts  # no live attempts, so no tokens
        assert a.stake == 0  # refunded: the allocator had to stake it 0
        targets = sorted(c.id for c in state.challenges.values() if c.round == 2 and c.author != a.attacker)
        assert state.stakes[f"2:{a.attacker}"][targets.index(a.challenge_id)] == 0
    for name, prov in provs.items():  # one call_charged per provider call: ghosts charge nothing
        charged = [e for e in event_log.read("d1") if e.type == ev.CALL_CHARGED and e.data["agent"] == name]
        assert len(charged) == len(prov.calls)


def test_ledger_preset_has_no_ghosts(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    assert preset("ledger").deep is None
    assert preset("ledger_deep").deep == DeepRule(ghosts=True)
    state: MatchState = new_state("l1")
    state.rules = preset("ledger").model_copy(update={"rounds": 2})
    make_engine().run(state)
    assert not _runs(event_log, "l1")
    assert not any(a.ghost for a in state.attacks.values())


def test_ghosts_run_before_allocation_and_show_in_the_allocate_prompt(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Recording(2), "bob": Recording(5)}
    state = make_engine(providers=provs).run(_deep(new_state))
    types = [e.type for e in event_log.read("d1")]
    r2 = [i for i, e in enumerate(event_log.read("d1")) if e.type == ev.GHOST_RUN]
    assert r2
    assert max(r2) < max(i for i, t in enumerate(types) if t == ev.STAKES_SET)  # round 2 stakes come after ghosts
    hit = [a for a in state.attacks.values() if a.ghost]
    assert hit
    for a in hit:
        prompts = [t for p, t in provs[a.attacker].calls if p == "allocate" and "ROUND: 2" in t]
        assert any(f"CAPTURED BY GHOST {a.ghost}" in t for t in prompts)


def test_fallback_split_skips_ghost_captured_targets() -> None:
    rule = preset("ledger_deep").ledger
    assert rule is not None
    stakes, fallback = settle_stakes([rule.min_alloc, 0, 0], 3, rule, frozenset({0}))
    assert fallback
    assert stakes[0] == 0
    assert sum(stakes) == rule.attack_tokens - rule.attack_tokens % 2
    assert settle_stakes([0, rule.min_alloc, 0], 3, rule, frozenset({0})) == ((0, rule.min_alloc, 0), False)


def _forged(ghost: str = "g", stake: int = 0, hits: dict[str, str] | None = None) -> RoundFacts:
    ch = Challenge(id="c0", round=1, author="a", spec=spec())
    atk = Attack(challenge_id="c0", attacker="b", solved=True, attempts=0, tokens_used=0, reason="r", stake=stake,
                 ghost=ghost)  # fmt: skip
    v = Validation(challenge_id="c0", valid=True, reason="r")
    return RoundFacts(1, ("a", "b"), (ch,), {"c0": v}, {"c0": atk}, ghost_hits=hits or {})


@pytest.mark.parametrize(
    "facts",
    [
        _forged(),  # plain ledger / deep off: no ghost hits at all
        _forged(hits={"c0": "other"}),  # deep on, but the log's hit is a different ghost
        _forged(stake=3000, hits={"c0": "g"}),  # a real hit, but staked (the refund was not taken)
    ],
    ids=["no-ghost-run", "wrong-ghost", "staked"],
)
def test_forged_ghost_captures_are_tampered(facts: RoundFacts) -> None:
    with pytest.raises(EventLogCorruptError, match="tampered"):
        SCORERS["ledger"](facts, ScoringRule())


def test_matching_ghost_capture_scores() -> None:
    assert SCORERS["ledger"](_forged(hits={"c0": "g"}), ScoringRule())["b"] >= 1


def test_ghost_captures_show_in_logged_points() -> None:
    facts = _forged(hits={"c0": "g"})
    rules = preset("ledger_deep")
    paid = capture_points(1, facts.challenges, facts.validations, facts.attacks, rules)
    assert paid == {"c0": ghosts.ghost_award(solve_award(spec(), 1, 1, rules.scoring))}


@pytest.mark.parametrize("ground", ["reverse", "rot13", "b64", "prefix:3"])
def test_ghost_replays_the_source_terrain_so_a_new_terrain_still_hits(new_state, ground: str) -> None:  # type: ignore[no-untyped-def]
    state: MatchState = new_state("t")
    state.challenges["c0"] = Challenge(id="c0", round=1, author="a", spec=spec())
    state.attacks["c0:b"] = Attack(challenge_id="c0", attacker="b", solved=True, attempts=1, tokens_used=1, reason="r")
    state.attempts["c0:b"] = [AttackAttempt(payload="open sesame", output=CAPTURED, tokens=1)]
    state.terrains["c0"] = ground
    [ghost] = ghosts.ghosts_before(state, 2)
    assert ghost.payload == terrain.apply(ground, "open sesame")  # the stdin that captured, under any later terrain


def test_verify_reruns_ghost_runs_and_catches_a_flipped_hit(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_deep(new_state))
    events = event_log.read("d1")
    sandbox = SubprocessSandbox(any_source)
    assert _runs(event_log)
    assert verify_ghosts(state, events, sandbox) == []
    i = next(i for i, e in enumerate(events) if e.type == ev.GHOST_RUN)
    events[i] = events[i].model_copy(update={"data": {**events[i].data, "hit": not events[i].data["hit"]}})
    assert verify_ghosts(state, events, sandbox) == [
        f"ghost run #{events[i].seq}: tampered: "
        f"{events[i].data['ghost_id']} -> {events[i].data['target']} does not reproduce"
    ]


def test_verify_recomputes_the_schedule_and_flags_a_missing_planned_run(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_deep(new_state))
    events = event_log.read("d1")
    sandbox = SubprocessSandbox(any_source)
    e = next(e for e in events if e.type == ev.GHOST_RUN)
    key = ghosts.run_key(int(e.data["round"]), e.data["ghost_id"], e.data["target"])
    dropped = [x for x in events if x is not e]
    assert f"ghost run {key}: tampered: planned run missing from the log" in verify_ghosts(state, dropped, sandbox)


def test_ghost_capture_with_zero_attempts_is_priced_as_one_try() -> None:
    facts = _forged(hits={"c0": "g"})
    one_try = solve_award(spec(), 1, 1, ScoringRule())
    assert SCORERS["classic"](facts, ScoringRule())["b"] == one_try  # not one_try + 1 for attempts=0
    assert SCORERS["ledger"](facts, ScoringRule())["b"] == ghosts.ghost_award(one_try)
