"""Sealed Ledger step 2 wiring: allocation, stakes, replay, fallback, stake-0 skip, tiebreak, estimate."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from pydantic import ValidationError

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.budget import BudgetLimits
from claude_duel.app.estimate import estimate
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.errors import BudgetExceededError
from claude_duel.domain.models import LedgerRule, MatchState, Rules, Stage, Usage
from claude_duel.domain.modes import preset
from claude_duel.domain.series import game_winner
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest


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
    assert all(a.tokens_used <= 12000 + 2000 for a in by_attacker["alice"])  # pre-call check: <= one call over
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


def test_ledger_rules_reject_unfundable_k() -> None:
    with pytest.raises(ValidationError, match="min_alloc"):
        Rules(challenges_per_round=4, ledger=LedgerRule(attack_tokens=3000, min_alloc=1000))
    assert Rules(challenges_per_round=3, ledger=LedgerRule(attack_tokens=3000, min_alloc=1000)).ledger


def _log_hash(event_log, match_id: str) -> str:  # type: ignore[no-untyped-def]
    """Hash of the log minus what differs per match id (created_seq, id-salted challenge ids)."""
    rows = [(e.type, {k: v for k, v in e.data.items() if k != "created_seq"}) for e in event_log.read(match_id)]
    # Attacks run concurrently, so order within a phase may interleave (pre-existing, not ledger): hash the sorted rows.
    lines = sorted(json.dumps(r, sort_keys=True) for r in rows)
    text = re.sub(r"(r\d+-\w+-\d+)-[0-9a-f]{8}", r"\1", "\n".join(lines))
    return hashlib.sha256(text.encode()).hexdigest()


def test_same_seed_same_log_and_scores(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    """Spec test 1: same seed + same fake outputs -> identical event log hash and final scores."""
    one = make_engine().run(_ledger(new_state, "d1"))
    two = make_engine().run(_ledger(new_state, "d2"))
    assert one.scores == two.scores
    assert _log_hash(event_log, "d1") == _log_hash(event_log, "d2")


def test_skipped_challenge_gets_no_attack_calls(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    """Spec test 6: a 0 stake makes no attack call for that challenge."""
    provs = {"alice": Allocating('{"stakes": [12000, 0, 0]}'), "bob": Allocating('{"stakes": [4000, 4000, 4000]}')}
    state = make_engine(providers=provs).run(_ledger(new_state))
    skipped = {a.challenge_id for a in state.attacks.values() if a.attacker == "alice" and a.stake == 0}
    assert len(skipped) == 4
    attack_prompts = [t for p, t in provs["alice"].prompts if p == "attack"]
    assert attack_prompts
    assert not any(cid in t for cid in skipped for t in attack_prompts)


def test_stakes_never_leak_before_reveal(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    """Review item 7: no round-1 prompt shows this round's stakes, own or the opponent's."""
    alice, bob = '{"stakes": [6007, 0, 5993]}', '{"stakes": [7001, 3011, 1988]}'
    provs = {"alice": Allocating(alice), "bob": Allocating(bob)}
    make_engine(providers=provs).run(_ledger(new_state))
    marks = ("6007", "5993", "7001", "3011", "1988")
    for prov in provs.values():
        round_one = [t for _, t in prov.prompts if "ROUND: 1" in t]
        assert round_one
        assert not [m for t in round_one for m in marks if m in t]


@pytest.mark.parametrize("calls", [2, 3, 5])
def test_ledger_match_stops_at_the_call_cap(make_engine, new_state, store, calls: int) -> None:  # type: ignore[no-untyped-def]
    """Spec test 10: the match cap (allocation calls included) aborts before the call that would exceed it."""
    metrics = InMemoryMetrics()
    with pytest.raises(BudgetExceededError):
        make_engine(budget=BudgetLimits(match_max_calls=calls), metrics=metrics).run(_ledger(new_state))
    assert store.load("l1").stage is Stage.FAILED


def test_keystones_sealed_then_revealed_and_replayed(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Allocating('{"stakes": [4000, 4000, 4000], "keystone": 1}'), "bob": Allocating("not json")}
    state = make_engine(providers=provs).run(_ledger(new_state))
    log = event_log.read("l1")
    types = [e.type for e in log]
    sealed = [e.data for e in log if e.type == ev.KEYSTONE_SEALED]
    assert [(d["round"], d["agent"]) for d in sealed] == [(1, "alice"), (1, "bob"), (2, "alice"), (2, "bob")]
    assert {d["agent"]: d["index"] for d in sealed if d["round"] == 1} == {"alice": 1, "bob": None}
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
    assert replay(state, log).consistent


def test_keystone_never_reaches_the_opponent_before_reveal(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    provs = {"alice": Allocating('{"stakes": [4000, 4000, 4000], "keystone": 2}'), "bob": Allocating("x")}
    make_engine(providers=provs).run(_ledger(new_state))
    secrets = [e.data["commit"] for e in event_log.read("l1") if e.type == ev.KEYSTONE_SEALED]
    secrets += [e.data["salt"] for e in event_log.read("l1") if e.type == ev.KEYSTONE_REVEALED]
    for _, prompt in provs["bob"].prompts + provs["alice"].prompts:
        assert not [s for s in secrets if s in prompt]
