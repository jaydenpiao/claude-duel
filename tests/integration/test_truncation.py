"""Truncated replies (#145): own failure reason, exactly one brevity repair call, symmetric output cap."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.app.engine import TRUNCATED
from claude_duel.domain import duplicate as dp
from claude_duel.domain import events as ev
from claude_duel.ports import Completion, CompletionRequest


class Cut:
    """Cuts the first ``cuts`` build replies off at the cap, then defers to the fake provider."""

    name = "cut"

    def __init__(self, cuts: int) -> None:
        self.cuts, self.fake, self.requests = cuts, FakeProvider(), list[CompletionRequest]()

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if request.purpose == "build" and self.cuts > 0:
            self.cuts -= 1
            return Completion(text='{"title": "half', input_tokens=10, output_tokens=request.max_tokens, truncated=True)
        return self.fake.complete(request)


def test_one_truncation_is_repaired_by_one_brief_retry(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Cut(1)
    state = make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    builds = [r for r in alice.requests if r.purpose == "build"]
    assert len(builds) == 2
    assert agents.TRUNCATED_HINT in builds[1].prompt
    mine = next(c for c in state.challenges.values() if c.author == "alice")
    assert state.validations[mine.id].valid
    assert mine.id not in state.build_errors  # the repair call fixed it; no rejection recorded


def test_two_truncations_are_their_own_failure_reason(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Cut(2)
    state = make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    mine = next(c for c in state.challenges.values() if c.author == "alice")
    assert state.build_errors[mine.id][0] == f"malformed: {TRUNCATED}"
    assert state.validations[mine.id].valid  # the next format retry succeeded
    assert not dp.build_truncated(state, mine.id)  # repaired: a later valid build is never a harness fault


def test_a_build_forfeited_on_truncation_is_typed_scored_and_counted(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    alice = Cut(100)  # every build reply is cut off
    state = make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    mine = next(c for c in state.challenges.values() if c.author == "alice")
    assert mine.spec is None
    rejected = [e.data for e in event_log.read(state.match_id) if e.type == ev.BUILD_REJECTED]
    assert rejected
    assert all(r["truncated"] is True for r in rejected)
    assert mine.id in state.build_truncated
    assert dp.build_truncated(state, mine.id)
    assert dp.truncations(state) == {"alice": 1}


def test_output_cap_and_schema_are_the_same_for_every_agent(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Cut(0), Cut(0)
    make_engine({"alice": alice, "bob": bob}).run(new_state(rounds=1, challenges_per_round=1, max_output_tokens=6000))
    reqs = alice.requests + bob.requests
    assert {r.max_tokens for r in reqs} == {6000}
    assert {r.purpose: r.json_schema for r in reqs} == {p: agents.ANSWER_SCHEMAS[p] for p in ("build", "attack")}


def test_wallet_cap_still_lowers_the_output_cap(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Cut(0)
    rules = {"rounds": 1, "challenges_per_round": 1, "wallet": {"max_output_tokens": 300}}
    make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(**rules))
    assert {r.max_tokens for r in alice.requests} == {300}


def test_answer_schemas_are_strict() -> None:
    for schema in agents.ANSWER_SCHEMAS.values():
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


class Thinks(Cut):
    """A reasoning model whose thinking eats the whole cap: empty text, all output tokens are reasoning."""

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose == "build" and self.cuts > 0:
            self.requests.append(request)
            self.cuts -= 1
            cap = request.max_tokens
            return Completion(text="", input_tokens=10, output_tokens=cap, reasoning_tokens=cap, truncated=True)
        return super().complete(request)


def test_reasoning_that_eats_the_cap_gets_one_brief_retry(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    alice = Thinks(1)
    state = make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    builds = [r for r in alice.requests if r.purpose == "build"]
    assert len(builds) == 2
    assert agents.TRUNCATED_HINT in builds[1].prompt
    mine = next(c for c in state.challenges.values() if c.author == "alice")
    assert state.validations[mine.id].valid
    assert mine.id not in state.build_errors


class Minimal(Cut):
    """Reports the effort it sent, as the OpenAI adapter does."""

    def complete(self, request: CompletionRequest) -> Completion:
        return super().complete(request).model_copy(update={"reasoning_effort": "minimal"})


def test_call_charged_logs_the_effort_sent(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine({"alice": Minimal(0), "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    charged = [e.data for e in event_log.read(state.match_id) if e.type == ev.CALL_CHARGED]
    assert {d["reasoning_effort"] for d in charged if d["agent"] == "alice"} == {"minimal"}
    assert {d["reasoning_effort"] for d in charged if d["agent"] == "bob"} == {None}
