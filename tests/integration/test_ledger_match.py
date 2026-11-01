"""Sealed Ledger step 2 wiring: allocation, stakes, replay, fallback, stake-0 skip, tiebreak, estimate."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from pydantic import ValidationError

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app.budget import DEFAULT_PRICES, BudgetLimits, CostBudget
from claude_duel.app.engine import MatchEngine
from claude_duel.app.estimate import estimate
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.models import LedgerRule, MatchState, Rules, ScoringRule, Stage, Usage
from claude_duel.domain.modes import preset
from claude_duel.domain.series import game_winner
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest
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
