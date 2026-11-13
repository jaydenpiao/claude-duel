"""Test-only helpers."""

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.domain import events as ev
from claude_duel.domain.heat import HeatScorer
from claude_duel.domain.heat_tokens import REF_CAP
from claude_duel.domain.models import AgentSpec, Event, LedgerRule, MatchState, Rules, SandboxLimits
from claude_duel.ports import Completion, CompletionRequest


def any_source(source: str) -> bool:
    """Lets tests run arbitrary (exploit) code in the subprocess sandbox. Never used outside tests."""
    return bool(source)


FLAG = "DUEL{0123456789abcdef0123456789abcdef}"


def match_events(*extra: Event) -> list[Event]:
    """A created event for a 1-round alice-vs-bob match, then ``extra``."""
    state = MatchState(
        match_id="m",
        secret="s",
        agents=(AgentSpec(name="alice"), AgentSpec(name="bob")),
        rules=Rules(rounds=1),
        limits=SandboxLimits(),
    )
    return [Event(seq=0, type=ev.MATCH_CREATED, trace_id="t", data=ev.created_data(state)), *extra]


def forfeit_events(reject_reason: str, failure: str) -> list[Event]:
    """alice forfeits, the arena rejects it, the match fails, then is recovered."""
    forfeit = {"id": "c1", "round": 1, "author": "alice", "spec": None, "forfeit_reason": f"malformed {FLAG}"}
    rejected = {"validation": {"challenge_id": "c1", "valid": False, "reason": reject_reason}}
    return match_events(
        Event(seq=1, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "build"}),
        Event(seq=2, type=ev.CHALLENGE_BUILT, trace_id="t", data={"challenge": forfeit}),
        Event(seq=3, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "validate"}),
        Event(seq=4, type=ev.CHALLENGE_VALIDATED, trace_id="t", data=rejected),
        Event(seq=5, type=ev.MATCH_FAILED, trace_id="t", data={"reason": failure}),
        Event(seq=6, type=ev.MATCH_RECOVERED, trace_id="t", data={}),
    )


ONE = {"rounds": 1, "challenges_per_round": 1}


class Builds:
    """Answers every build with one fixed challenge; attacks go to the fake provider."""

    name = "builds"

    def __init__(self, **spec: object) -> None:
        self.text = json.dumps({"title": "t", "description": "d", "difficulty": 1, **spec})
        self.fake = FakeProvider()

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose != "build":
            return self.fake.complete(request)
        return Completion(text=self.text, input_tokens=10, output_tokens=10)


def run_bob(make_engine, new_state, metrics=None, **spec):  # type: ignore[no-untyped-def]
    """Runs a 1-challenge match where bob builds ``spec``; returns the state and bob's challenge."""
    engine = make_engine({"alice": FakeProvider(), "bob": Builds(**spec)}, metrics=metrics)
    state = engine.run(new_state(**ONE))
    return state, next(c for c in state.challenges.values() if c.author == "bob")


def similarity(a: str, b: str, *, token_cap: int | None = None, scorer: HeatScorer | None = None) -> int:
    """One heat pair: legacy (exact while both walks fit, else ``HeatBoundError``) or the prefix of ``token_cap``."""
    caps = {} if token_cap is None else {"heat_token_cap": token_cap, "heat_ref_cap": REF_CAP}
    return (scorer or HeatScorer()).similarity(a, b, LedgerRule(heat_window=1, **caps)).pct
