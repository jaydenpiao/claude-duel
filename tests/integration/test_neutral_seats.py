"""Neutral seats: under the ladder preset no prompt, memory line or challenge id carries an agent, model or vendor
name; models see only "p1" (leads this match) and "p2". Older rules keep name-based ids, so old logs replay."""

from __future__ import annotations

import re

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.replay import replay
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.domain.modes import preset
from claude_duel.ports import Completion, CompletionRequest

NAMES = ("claude-haiku-5-5", "gpt-6-luna")
LEAKS = re.compile(r"claude|gpt|haiku|luna|anthropic|openai|fake-1|alice|bob", re.IGNORECASE)


class Recording:
    """The fake provider, keeping every request it was sent."""

    name = "fake"

    def __init__(self, skill: int) -> None:
        self.fake, self.requests = FakeProvider(skill=skill), list[CompletionRequest]()

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return self.fake.complete(request)


def _state(rules: Rules, names: tuple[str, str] = NAMES) -> MatchState:
    agents = (AgentSpec(name=names[0], model="fake-1"), AgentSpec(name=names[1], model="fake-1"))
    return MatchState(match_id="seat-1", secret="s" * 64, agents=agents, rules=rules, limits=SandboxLimits())


def test_ladder_prompts_and_challenge_ids_name_seats_only(make_engine, event_log) -> None:  # type: ignore[no-untyped-def]
    rules = preset("ladder").model_copy(update={"rounds": 2})  # two rounds, so round 2 carries memory
    assert rules.neutral_ids
    providers = {NAMES[0]: Recording(2), NAMES[1]: Recording(5)}
    state = make_engine(providers).run(_state(rules))
    sent = [r for p in providers.values() for r in p.requests]
    assert sent
    for r in sent:
        assert not LEAKS.search(r.system), r.system[:200]
        assert not LEAKS.search(r.prompt), r.prompt[:400]
    assert {line for r in sent for line in r.prompt.splitlines() if line.startswith("AGENT:")} == {
        "AGENT: p1",
        "AGENT: p2",
    }
    assert state.challenges
    assert all(re.fullmatch(r"r\d-p[12]-\d-[0-9a-f]{8}", cid) for cid in state.challenges)
    assert {c.author for c in state.challenges.values()} == set(NAMES)  # logs and display keep the real names
    assert replay(state, event_log.read(state.match_id)).consistent


def test_seats_follow_the_seat_not_the_model() -> None:
    rules = preset("ladder")
    first, swapped = _state(rules), _state(rules, (NAMES[1], NAMES[0]))
    assert (first.seat(NAMES[0]), first.seat(NAMES[1])) == ("p1", "p2")
    assert (swapped.seat(NAMES[1]), swapped.seat(NAMES[0])) == ("p1", "p2")


def test_rules_without_neutral_ids_keep_name_based_ids(make_engine) -> None:  # type: ignore[no-untyped-def]
    rules = Rules(rounds=1, challenges_per_round=1)
    assert not rules.neutral_ids
    state = make_engine().run(_state(rules, ("alice", "bob")))
    assert any("-alice-" in cid for cid in state.challenges)
    assert state.seat("alice") == "alice"
