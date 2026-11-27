"""S18-6 regression: min_try_units gates a try on the stake's output room only. The attack prompt carries the
opponent's source and description; padding both (still valid) must not cost the attacker a try. $0, fake providers."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.domain.models import AgentSpec, MatchState, SandboxLimits
from claude_duel.domain.modes import preset
from claude_duel.ports import Completion, CompletionRequest

OUT = 3000


class Attacker:
    name = "fake"

    def __init__(self) -> None:
        self.fake = FakeProvider(skill=0)

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose == "allocate":
            out = self.fake.complete(request)
            return out.model_copy(update={"text": json.dumps({"stakes": [6000], "keystone": 0})})
        out = self.fake.complete(request)
        if request.purpose == "attack":
            return out.model_copy(update={"output_tokens": min(OUT, request.max_tokens)})
        return out


class PaddingAuthor:
    name = "fake"

    def __init__(self, pad: bool, limit: int) -> None:
        self.fake, self.pad, self.limit = FakeProvider(skill=0), pad, limit

    def complete(self, request: CompletionRequest) -> Completion:
        out = self.fake.complete(request)
        if request.purpose != "build" or not self.pad:
            return out
        body = json.loads(out.text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        room = self.limit - len(body["source"].encode()) - 400  # stay under the cap after the arena wrapper
        body["source"] = body["source"] + "\n#" + "=" * max(room, 0) + "\n"
        body["description"] = (body["description"] + " " + "=" * 2000)[:2000]
        return out.model_copy(update={"text": "```json\n" + json.dumps(body) + "\n```"})


def _run(make_engine, pad: bool):  # type: ignore[no-untyped-def]
    rules = preset("ladder")
    agents = (AgentSpec(name="alice", model="fake-1"), AgentSpec(name="bob", model="fake-1"))
    state = MatchState(match_id=f"mt-{pad}", secret="s" * 64, agents=agents, rules=rules, limits=SandboxLimits())
    state = make_engine({"alice": Attacker(), "bob": PaddingAuthor(pad, rules.max_source_bytes(1))}).run(state)
    attacks = [a for a in state.attacks.values() if a.attacker == "alice"]
    return attacks, [c for c in state.challenges.values() if c.author == "bob"]


def test_a_padded_valid_challenge_does_not_cost_the_attackers_tries(make_engine) -> None:  # type: ignore[no-untyped-def]
    clean, _ = _run(make_engine, False)
    padded, pc = _run(make_engine, True)
    assert pc
    assert all(c.spec is not None for c in pc)  # the padded challenges are still valid
    assert sum(len(c.spec.source.encode()) for c in pc if c.spec) > 1500 * len(pc)  # and really long
    assert [a.attempts for a in padded] == [a.attempts for a in clean]
    assert all(a.attempts >= 1 for a in padded)  # a min_alloc stake always gets its first try
