"""Sealed Ledger step 2 wiring: allocation, stakes, replay, fallback, stake-0 skip, tiebreak, estimate."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from pydantic import ValidationError
from rich.console import Console

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.app.estimate import estimate
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain import stakes
from claude_duel.domain.models import LedgerRule, MatchState, Rules, ScoringRule, Stage, Usage
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import capture_points
from claude_duel.domain.series import game_winner
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest
from claude_duel.presentation.arena import ArenaView
from tests.helpers import any_source


class Allocating:
    """Fake provider whose allocation answer is fixed text; everything else is the plain fake."""

    name = "allocating"

    def __init__(self, text: str, skill: int = 2) -> None:
        self.inner, self.text, self.prompts = FakeProvider(skill=skill), text, []  # type: ignore[var-annotated]

    def complete(self, request: CompletionRequest) -> Completion:
        self.prompts.append((request.purpose, request.prompt))
        if request.purpose == "allocate":
            return Completion(text=self.text, input_tokens=10, output_tokens=5)
        return self.inner.complete(request)


def _ledger(new_state, match_id: str = "l1") -> MatchState:  # type: ignore[no-untyped-def]
    state: MatchState = new_state(match_id)
    state.rules = preset("ledger").model_copy(update={"rounds": 2})
    return state


def test_ledger_match_completes_and_replays(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_ledger(new_state))
    assert state.stage is Stage.COMPLETE
    staked = [e for e in event_log.read("l1") if e.type == ev.STAKES_SET]
    assert [(e.data["round"], e.data["agent"]) for e in staked] == [(1, "alice"), (1, "bob"), (2, "alice"), (2, "bob")]
    assert all(e.data["stakes"] == [4000, 4000, 4000] and not e.data["fallback"] for e in staked)
    assert all(a.stake == 4000 for a in state.attacks.values())
    assert replay(state, event_log.read("l1")).consistent


def test_bad_allocation_falls_back_to_even_split(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    bad = {"alice": Allocating('{"stakes": [12000, 12000, 0]}'), "bob": Allocating("not json")}
    state = make_engine(providers=bad, metrics=metrics).run(_ledger(new_state))
    staked = [e for e in event_log.read("l1") if e.type == ev.STAKES_SET]
    assert all(e.data["fallback"] and e.data["stakes"] == [4000, 4000, 4000] for e in staked)
    assert metrics.counters["ledger.alloc_fallback"] == 4
    assert state.stage is Stage.COMPLETE


def test_stake_zero_skips_attack_and_caps_spend(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Allocating(json.dumps({"stakes": [12000, 0, 0]})), "bob": Allocating('{"stakes": [0, 0, 0]}')}
    state = make_engine(providers=provs).run(_ledger(new_state))
    by_attacker = {n: [a for a in state.attacks.values() if a.attacker == n] for n in ("alice", "bob")}
    assert all(a.stake == 0 and a.attempts == 0 and not a.solved for a in by_attacker["bob"])
    assert sum(a.stake == 0 for a in by_attacker["alice"]) == 2 * 2
    assert not any(p == "attack" and "AGENT: bob" in t for p, t in provs["bob"].prompts)
    assert all(a.stake_overshoot == max(0, a.tokens_used - 12000) for a in by_attacker["alice"])
    assert all(a.stake_overshoot == 0 for a in by_attacker["alice"])  # fake input = len/4 <= the estimate
    assert replay(state, event_log.read("l1")).consistent


def test_allocate_prompt_shows_sources_and_never_the_flag(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Allocating('{"stakes": [4000, 4000, 4000]}'), "bob": Allocating('{"stakes": [4000, 4000, 4000]}')}
    make_engine(providers=provs).run(_ledger(new_state))
    prompt = next(t for p, t in provs["alice"].prompts if p == "allocate")
    for line in ("TASK: ALLOCATE", "BUDGET: 12000", "CHALLENGES: 3"):
        assert line in prompt
    assert "<<<UNTRUSTED" in prompt


def test_ledger_tie_goes_to_lower_spend_then_tokens(new_state) -> None:  # type: ignore[no-untyped-def]
    state = _ledger(new_state)
    state.scores = {"alice": 10, "bob": 10}
    state.usage_by_agent = {"alice": Usage(usd=0.002, tokens=900), "bob": Usage(usd=0.001, tokens=5000)}
    assert game_winner(state) == "bob"
    state.usage_by_agent = {"alice": Usage(usd=0.001, tokens=900), "bob": Usage(usd=0.001, tokens=5000)}
    assert game_winner(state) == "alice"
    state.usage_by_agent = {"alice": Usage(usd=0.001, tokens=900), "bob": Usage(usd=0.001, tokens=900)}
    assert game_winner(state) is None


def test_estimate_ledger_within_match_cap() -> None:
    limits = BudgetLimits()
    assert 0 < estimate(preset("ledger"), "gpt-5-nano", limits).usd <= limits.match_max_usd


def test_estimate_ledger_bounded_by_stakes() -> None:
    roomy = BudgetLimits(match_max_tokens=10_000_000, match_max_calls=10_000)
    row = estimate(preset("ledger"), "gpt-5-nano", roomy)
    plain = estimate(preset("ledger").model_copy(update={"ledger": None}), "gpt-5-nano", roomy)
    assert row.bound == "stakes"
    assert row.tokens < plain.tokens


LEDGER = ScoringRule(kind="ledger")


def test_ledger_rules_reject_unfundable_k() -> None:
    with pytest.raises(ValidationError, match="min_alloc"):
        Rules(challenges_per_round=4, scoring=LEDGER, ledger=LedgerRule(attack_tokens=3000, min_alloc=1000))
    assert Rules(challenges_per_round=3, scoring=LEDGER, ledger=LedgerRule(attack_tokens=3000, min_alloc=1000)).ledger


def test_ledger_scoring_needs_ledger_rules() -> None:
    with pytest.raises(ValidationError, match='mode = "ledger"'):
        Rules(scoring=LEDGER)


def test_ledger_rules_need_ledger_scoring() -> None:
    with pytest.raises(ValidationError, match='mode = "ledger"'):
        Rules(ledger=LedgerRule())


def _log_hash(event_log, match_id: str) -> str:  # type: ignore[no-untyped-def]
    """Hash of the log minus what differs per match id (created_seq, id-salted challenge ids)."""
    return hashlib.sha256("\n".join(_log_rows(event_log, match_id)).encode()).hexdigest()


def _log_rows(event_log, match_id: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Rows in log order (ordering is part of what's tested), minus per-match ids and wall-clock latency."""
    drop = {"created_seq", "elapsed_ms"}
    rows = [(e.type, {k: v for k, v in e.data.items() if k not in drop}) for e in event_log.read(match_id)]
    return [re.sub(r"(r\d+-\w+-\d+)-[0-9a-f]{8}", r"\1", json.dumps(r, sort_keys=True)) for r in rows]


def test_same_seed_same_log_and_scores(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """Spec test 1: same seed + same fake outputs -> identical event log hash and final scores."""
    one = make_engine().run(_ledger(new_state, "d1"))
    two = make_engine().run(_ledger(new_state, "d2"))
    assert one.scores == two.scores
    a, b = _log_rows(event_log, "d1"), _log_rows(event_log, "d2")
    first = next(((i, x, y) for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), None)
    assert first is None, f"first differing row: {first}"
    assert len(a) == len(b)
    assert _log_hash(event_log, "d1") == _log_hash(event_log, "d2")  # replay consistency: test_ledger_match_completes


def test_skipped_challenge_gets_no_attack_calls(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    """Spec test 6: a 0 stake makes no attack call for that challenge."""
    provs = {"alice": Allocating('{"stakes": [12000, 0, 0]}'), "bob": Allocating('{"stakes": [4000, 4000, 4000]}')}
    state = make_engine(providers=provs).run(_ledger(new_state))
    skipped = {a.challenge_id for a in state.attacks.values() if a.attacker == "alice" and a.stake == 0}
    assert len(skipped) == 4
    attack_prompts = [t for p, t in provs["alice"].prompts if p == "attack"]
    assert attack_prompts
    assert not any(cid in t for cid in skipped for t in attack_prompts)


def _round_one(prompts: list[tuple[str, str]]) -> list[str]:
    """An agent's prompts before its first round-2 prompt, with id-salted challenge ids normalized."""
    out = []
    for _, text in prompts:
        if "ROUND: 2" in text:
            break
        out.append(re.sub(r"(r\d+-\w+-\d+)-[0-9a-f]{8}", r"\1", text))
    return out


def test_stakes_never_leak_before_reveal(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    """Spec test 4/7, differential: alice's round-1 allocation changes nothing bob sees in round 1."""
    seen = []
    for mid, alice in (("x1", '{"stakes": [6000, 0, 6000]}'), ("x2", '{"stakes": [1000, 9000, 2000]}')):
        provs = {"alice": Allocating(alice), "bob": Allocating('{"stakes": [4000, 4000, 4000]}')}
        make_engine(providers=provs).run(_ledger(new_state, mid))
        seen.append(_round_one(provs["bob"].prompts))
    assert len(seen[0]) > 3
    assert seen[0] == seen[1]


@pytest.mark.parametrize("calls", [2, 3, 5])
def test_ledger_match_stops_at_the_call_cap(make_engine, new_state, store, calls: int) -> None:  # type: ignore[no-untyped-def]
    """Spec test 10: the match cap (allocation calls included) aborts before the call that would exceed it."""
    metrics = InMemoryMetrics()
    state = make_engine(budget=BudgetLimits(match_max_calls=calls), metrics=metrics).run(_ledger(new_state))
    assert state.stage is Stage.CAPPED  # ADR 0014 (#171)
    assert store.load("l1").stage is Stage.CAPPED
    assert state.usage.calls == calls


def test_keystones_sealed_then_revealed_and_replayed(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Allocating('{"stakes": [4000, 4000, 4000], "keystone": 1}'), "bob": Allocating("not json")}
    state = make_engine(providers=provs).run(_ledger(new_state))
    log = event_log.read("l1")
    types = [e.type for e in log]
    sealed = [e.data for e in log if e.type == ev.KEYSTONE_SEALED]
    assert [(d["round"], d["agent"]) for d in sealed] == [(1, "alice"), (1, "bob"), (2, "alice"), (2, "bob")]
    opened = [e.data for e in log if e.type == ev.KEYSTONE_REVEALED]
    assert {d["agent"]: d["index"] for d in opened if d["round"] == 1} == {"alice": 1, "bob": None}
    for r in (1, 2):  # reveal comes after every attack of the round and before its score
        reveals = [i for i, e in enumerate(log) if e.type == ev.KEYSTONE_REVEALED and e.data["round"] == r]
        attacks = [
            i for i, e in enumerate(log) if e.type == ev.CHALLENGE_ATTACKED and e.data["key"].startswith(f"r{r}-")
        ]
        scored = next(i for i, e in enumerate(log) if e.type == ev.ROUND_SCORED and e.data["round"] == r)
        assert len(reveals) == 2
        assert max(attacks) < min(reveals)
        assert max(reveals) < scored
    assert all(ks.revealed for ks in state.keystones.values())
    assert types.count(ev.KEYSTONE_REVEALED) == 4
    assert all("index" not in e.data for e in log if e.type == ev.KEYSTONE_SEALED)  # the seal logs only the commit
    assert replay(state, log).consistent


def test_keystone_never_reaches_the_opponent_before_reveal(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    """Spec test 4, differential: alice's keystone index changes nothing bob sees in round 1."""
    seen = []
    for mid, pick in (("k1", 0), ("k2", 2)):
        alice = Allocating(json.dumps({"stakes": [4000, 4000, 4000], "keystone": pick}))
        provs = {"alice": alice, "bob": Allocating('{"stakes": [4000, 4000, 4000], "keystone": 1}')}
        state = make_engine(providers=provs).run(_ledger(new_state, mid))
        assert state.keystones["1:alice"].index == pick
        seen.append(_round_one(provs["bob"].prompts))
    assert len(seen[0]) > 3
    assert seen[0] == seen[1]


def test_ledger_match_stops_at_the_dollar_cap(new_state, store, event_log, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Spec test 10 (dollars): priced like gpt-5-nano, allocate calls included, the match cap aborts the match
    once spend reaches it, before any further call. It ends CAPPED (ADR 0014)."""
    spend = FileSpendLedger(tmp_path / "spend.jsonl")
    cap = 0.0004
    engine = MatchEngine(
        {"alice": FakeProvider(skill=2), "bob": FakeProvider(skill=5)},
        SubprocessSandbox(any_source),
        store,
        event_log,
        InMemoryMetrics(),
        CostBudget(BudgetLimits(match_max_usd=cap), spend, DEFAULT_PRICES),
        dict.fromkeys(("alice", "bob"), "gpt-5-nano"),
    )
    assert engine.run(_ledger(new_state)).stage is Stage.CAPPED
    assert store.load("l1").stage is Stage.CAPPED
    charged = [e.data for e in event_log.read("l1") if e.type == ev.CALL_CHARGED]
    usd = [d["usd"] for d in charged]
    assert usd
    assert sum(usd[:-1]) < cap <= sum(usd)  # checked before each call: the last call is the one that crossed it


def test_no_attack_call_when_the_stake_left_is_below_the_floor(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    """Review #192 (a): max_tokens is fitted to the stake; under STAKE_FLOOR the attack stops with no call."""
    provs = {"alice": Allocating('{"stakes": [300, 300, 300]}'), "bob": Allocating('{"stakes": [0, 0, 0]}')}
    st0 = _ledger(new_state)
    assert st0.rules.ledger is not None
    small = st0.rules.ledger.model_copy(update={"min_alloc": 100})
    st0.rules = st0.rules.model_copy(update={"ledger": small})
    state = make_engine(providers=provs).run(st0)
    mine = [a for a in state.attacks.values() if a.attacker == "alice"]
    assert all(a.reason == "stake exhausted" and a.attempts == 0 for a in mine)
    assert not [p for p, _ in provs["alice"].prompts if p == "attack"]


def test_keystone_pick_shows_sources_and_memory_recalls_past_keystones(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    provs = {
        "alice": Allocating('{"stakes": [4000, 4000, 4000], "keystone": 1}'),
        "bob": Allocating('{"stakes": [4000, 4000, 4000], "keystone": 0}'),
    }
    state = make_engine(providers=provs).run(_ledger(new_state))
    alloc = [p for purpose, p in provs["alice"].prompts if purpose == "allocate"]
    assert len(alloc) == 2
    own = stakes.own_challenges(state.challenges, 1, "alice")
    assert all(c.spec is not None and c.spec.source.strip().splitlines()[0] in alloc[0] for c in own)
    assert "(keystone)" not in alloc[0]  # nothing to recall in round 1
    assert alloc[1].count("(keystone)") == 2  # round 2 memory: alice's and bob's revealed round-1 keystones


def test_terrain_drawn_before_allocation_and_applied_to_stdin(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine().run(_ledger(new_state))
    log = event_log.read("l1")
    drawn = [i for i, e in enumerate(log) if e.type == ev.TERRAIN_DRAWN]
    allocs = [i for i, e in enumerate(log) if e.type == ev.STAKES_SET]
    assert len(drawn) == 2
    assert drawn[0] < allocs[0]
    assert set(state.terrains) == {c.id for c in state.challenges.values() if state.validations[c.id].valid}
    assert any(a.solved for a in state.attacks.values())  # the fake inverts the terrain, so captures still happen
    assert replay(state, log).consistent


def test_terrain_none_logs_nothing(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    st0 = _ledger(new_state)
    rules = st0.rules.model_copy(update={"ledger": st0.rules.ledger.model_copy(update={"terrain": "none"})})
    state = make_engine().run(st0.model_copy(update={"rules": rules}))
    assert not state.terrains
    assert ev.TERRAIN_DRAWN not in [e.type for e in event_log.read("l1")]


def test_round_scored_logs_points_per_capture(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """Each staked capture's points are on round_scored; replay rejects a forged value."""
    state = make_engine().run(_ledger(new_state))
    log = event_log.read("l1")
    points = {cid: p for e in log if e.type == ev.ROUND_SCORED for cid, p in e.data["points"].items()}
    assert set(points) == {a.challenge_id for a in state.attacks.values() if a.solved and a.stake}
    assert points
    assert all(p >= 1 for p in points.values())
    assert replay(state, log).consistent
    forged = [
        e.model_copy(update={"data": {**e.data, "points": {k: v + 1 for k, v in e.data["points"].items()}}})
        if e.type == ev.ROUND_SCORED
        else e
        for e in log
    ]
    assert "round 1 points tampered" in replay(state, forged).mismatches


def test_arena_skips_a_forged_capture_line(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """A points entry naming a challenge with no attack (only a forged log) is skipped, not a crash."""
    make_engine().run(_ledger(new_state))
    view = ArenaView(Console(record=True, width=100), "l1", 50.0, lambda: 0.0, pace=0.001)
    for e in event_log.read("l1"):
        view(e.model_copy(update={"data": {**e.data, "points": {"nope": 3}}}) if e.type == ev.ROUND_SCORED else e)
    view.close()


def test_no_capture_points_outside_ledger() -> None:
    assert capture_points(1, [], {}, {}, Rules()) == {}


class Heavy(Allocating):
    """Reports more input tokens than the engine's len/4 estimate, so spend can pass the stake."""

    def complete(self, request: CompletionRequest) -> Completion:
        c = super().complete(request)
        extra = 3_217 if request.purpose == "attack" else 0
        return c.model_copy(update={"input_tokens": c.input_tokens + extra})


def test_stake_overshoot_records_the_estimate_gap(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """Real input counts above the estimate: overshoot is exactly tokens past the stake, and never folds into score."""
    provs = {"alice": Heavy(json.dumps({"stakes": [3000, 0, 0]}), skill=0), "bob": Allocating('{"stakes": [0, 0, 0]}')}
    state = make_engine(providers=provs).run(_ledger(new_state))
    staked = [a for a in state.attacks.values() if a.attacker == "alice" and a.stake]
    assert staked
    assert all(a.stake_overshoot == max(0, a.tokens_used - 3000) for a in staked)
    assert any(a.stake_overshoot > 0 for a in staked)
    assert replay(state, event_log.read("l1")).consistent


def test_pre_keystone_ledger_log_replays_with_features_off(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """An old ledger log pins `rules.ledger` without keystone, terrain or heat; it folds to off and replays."""
    st0 = _ledger(new_state)
    old = LedgerRule(attack_tokens=st0.rules.ledger.attack_tokens, min_alloc=st0.rules.ledger.min_alloc)
    state = make_engine().run(st0.model_copy(update={"rules": st0.rules.model_copy(update={"ledger": old})}))
    log = event_log.read("l1")
    pinned = log[0].data["rules"]["ledger"]
    for gone in ("keystone", "terrain", "heat_window"):
        del pinned[gone]
    folded = ev.initial_state("l1", log[0]).rules.ledger
    assert folded is not None
    assert (folded.keystone, folded.terrain, folded.heat_window) == (False, "none", 0)
    assert replay(state, log).consistent


def test_ledger_preset_turns_the_features_on() -> None:
    rule = preset("ledger").ledger
    assert rule is not None
    assert (rule.keystone, rule.terrain, rule.heat_window, rule.heat_vs_opponent) == (True, "seeded", 2, True)


def test_pre_points_ledger_log_replays_clean(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """A ledger log from before #199 has no round_scored points: replay skips that check, not flags it."""
    state = make_engine().run(_ledger(new_state))
    old = [
        e.model_copy(update={"data": {k: v for k, v in e.data.items() if k != "points"}})
        if e.type == ev.ROUND_SCORED
        else e
        for e in event_log.read("l1")
    ]
    assert any(a.solved and a.stake for a in state.attacks.values())
    result = replay(state, old)
    assert result.consistent, result.mismatches


def test_missing_stakes_skip_attack_not_unlimited(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """Ledger with no stakes for an attacker must skip attacks (stake 0), not leave them uncapped (#255)."""
    eng = make_engine()
    eng._allocate = lambda state, attacker: None  # type: ignore[method-assign]
    state = eng.run(_ledger(new_state, "no-stakes"))
    assert state.stage is Stage.COMPLETE
    assert state.stakes == {}
    assert state.attacks
    assert all(a.stake == 0 and a.attempts == 0 and a.reason == "skipped: stake 0" for a in state.attacks.values())
    # no attack calls: every challenge was skipped at stake 0
    assert not any(e.type == ev.ATTACK_STARTED for e in event_log.read("no-stakes"))


def test_resume_does_not_repay_allocate(make_engine, new_state, store, event_log) -> None:  # type: ignore[no-untyped-def]
    """Crash after allocate is charged but before stakes land: resume settles even-split, no second pay (#254)."""
    counts: dict[str, int] = {"alice": 0, "bob": 0}

    class Counting(Allocating):
        def __init__(self, name: str, text: str) -> None:
            super().__init__(text)
            self.who = name

        def complete(self, request: CompletionRequest) -> Completion:
            if request.purpose == "allocate":
                counts[self.who] += 1
            return super().complete(request)

    text = '{"stakes": [4000, 4000, 4000]}'
    eng = make_engine(providers={"alice": Counting("alice", text), "bob": Counting("bob", text)})
    orig = eng._checkpoint
    stakes_seen = {"n": 0}

    def boom(state: MatchState, kind: str, **data: object) -> None:
        if kind == ev.STAKES_SET:
            stakes_seen["n"] += 1
            if stakes_seen["n"] == 1:
                raise RuntimeError("crash after allocate paid")
        orig(state, kind, **data)

    eng._checkpoint = boom  # type: ignore[method-assign]
    state0 = _ledger(new_state, "resume-alloc")
    state0.rules = state0.rules.model_copy(update={"rounds": 1})
    with pytest.raises(RuntimeError, match="crash after allocate paid"):
        eng.run(state0)
    saved = store.load("resume-alloc")
    assert "1:alice" in saved.allocate_started
    assert "1:alice" not in saved.stakes
    assert counts["alice"] == 1
    done = make_engine(providers={"alice": Counting("alice", text), "bob": Counting("bob", text)}).run(saved)
    assert done.stage is Stage.COMPLETE
    assert counts["alice"] == 1  # no second allocate pay for alice
    assert "1:alice" in done.stakes
    alice_r1 = next(
        e
        for e in event_log.read("resume-alloc")
        if e.type == ev.STAKES_SET and e.data["agent"] == "alice" and e.data["round"] == 1
    )
    assert alice_r1.data["fallback"] is True
    assert alice_r1.data["stakes"] == [4000, 4000, 4000]
    assert replay(done, event_log.read("resume-alloc")).consistent
