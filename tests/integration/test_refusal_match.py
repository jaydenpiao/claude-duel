"""A refused build is a forfeit; a refused attack is one spent try (ADR 0016)."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.app.engine import REFUSED
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.models import Stage
from claude_duel.domain.scoring import defense_award
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest
from claude_duel.presentation.card import card_data, card_svg
from tests.integration.test_build_repair import Scripted


class Refuse:
    """Forwards to a fake provider except for one purpose, which is refused (and counted)."""

    name = "refuse"

    def __init__(self, purpose: str) -> None:
        self.purpose, self.fake, self.calls, self.refused = purpose, FakeProvider(), 0, 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        if request.purpose == self.purpose:
            self.refused += 1
            return Completion(text="", input_tokens=4, output_tokens=1, refused=True)
        return self.fake.complete(request)


class RefuseThenSolve:
    """Refuse the first attack, then send the maze example payload."""

    name = "refuse-then-solve"

    def __init__(self) -> None:
        self.fake, self.calls, self.refused, self.attacks = FakeProvider(), 0, 0, 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        if request.purpose != "attack":
            return self.fake.complete(request)
        self.attacks += 1
        if self.attacks == 1:
            self.refused += 1
            return Completion(text="", input_tokens=4, output_tokens=1, refused=True)
        body = "```json\n" + json.dumps({"payload": "DDRR"}) + "\n```"
        return Completion(text=body, input_tokens=4, output_tokens=4)


def _calls(events: list[ev.Event], purpose: str | None = None) -> list[dict[str, object]]:
    out = []
    for e in events:
        if e.type != ev.CALL_CHARGED:
            continue
        if purpose is None or e.data.get("purpose") == purpose:
            out.append(e.data)
    return out


def test_refused_build_is_a_forfeit_and_replays(make_engine, new_state, store, event_log) -> None:  # type: ignore[no-untyped-def]
    alice = Refuse("build")
    engine = make_engine({"alice": alice, "bob": FakeProvider()})
    state = engine.run(new_state(rounds=1, challenges_per_round=1))
    mine = next(c for c in state.challenges.values() if c.author == "alice")
    assert mine.spec is None
    assert mine.forfeit_reason == REFUSED
    assert not state.validations[mine.id].valid
    assert state.validations[mine.id].reason == REFUSED
    # forfeit line is the invalid penalty; alice may still score on bob's challenge
    assert state.scores["alice"] >= -state.rules.scoring.invalid_penalty
    builds = _calls(event_log.read(state.match_id), "build")
    alice_builds = [c for c in builds if c["agent"] == "alice"]
    assert alice.refused == 1
    assert len(alice_builds) == 1
    assert alice_builds[0]["refused"] is True
    assert all("refused" not in c for c in builds if c["agent"] != "alice")
    report = replay(state, event_log.read(state.match_id))
    assert report.consistent
    assert report.refusals == 1
    again = engine.run(store.load(state.match_id))
    assert again.stage is Stage.COMPLETE
    assert again.usage.calls == state.usage.calls
    assert alice.refused == 1  # resume does not re-pay
    svg = card_svg(card_data(again, replay(again, event_log.read(again.match_id)), event_log.read(again.match_id)))
    assert "1 model refusal" in svg


def test_refused_attack_is_one_spent_try_and_resumes(make_engine, new_state, store, event_log) -> None:  # type: ignore[no-untyped-def]
    alice = Refuse("attack")
    engine = make_engine({"alice": alice, "bob": FakeProvider()})
    state = engine.run(new_state(rounds=1, challenges_per_round=1, max_attempts=3))
    mine = next(a for a in state.attacks.values() if a.attacker == "alice")
    assert not mine.solved
    assert mine.reason == "attempts exhausted"
    assert mine.attempts == 3
    attempts = [e for e in event_log.read(state.match_id) if e.type == ev.ATTACK_ATTEMPTED]
    alice_tries = [e.data["attempt"] for e in attempts if str(e.data["key"]).endswith(":alice")]
    assert len(alice_tries) == 3
    assert all(t["output"] == REFUSED and t["refused"] is True for t in alice_tries)
    attacks = _calls(event_log.read(state.match_id), "attack")
    alice_calls = [c for c in attacks if c["agent"] == "alice"]
    assert len(alice_calls) == 3
    assert all(c.get("refused") is True for c in alice_calls)
    assert all("refused" not in c for c in attacks if c["agent"] != "alice")
    bob = next(c for c in state.challenges.values() if c.author == "bob")
    assert bob.spec is not None
    assert state.validations[bob.id].valid
    assert state.scores["bob"] >= defense_award(bob.spec, bob.round, state.rules.scoring)
    report = replay(state, event_log.read(state.match_id))
    assert report.consistent
    assert report.refusals == 3
    again = engine.run(store.load(state.match_id))
    assert again.usage.calls == state.usage.calls
    assert alice.refused == 3
    svg = card_svg(card_data(again, replay(again, event_log.read(again.match_id)), event_log.read(again.match_id)))
    assert "3 model refusals" in svg


def test_attack_continues_after_a_refused_try(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    alice = RefuseThenSolve()
    bob = Scripted(json.dumps(agents.EXAMPLE_CHALLENGE))
    state = make_engine({"alice": alice, "bob": bob}).run(new_state(rounds=1, challenges_per_round=1, max_attempts=3))
    mine = next(a for a in state.attacks.values() if a.attacker == "alice")
    alice_tries = [
        e.data["attempt"]
        for e in event_log.read(state.match_id)
        if e.type == ev.ATTACK_ATTEMPTED and str(e.data["key"]).endswith(":alice")
    ]
    assert alice.refused == 1
    assert len(alice_tries) >= 2
    assert alice_tries[0]["refused"] is True
    assert "refused" not in alice_tries[1]
    assert mine.solved
    assert mine.reason == "flag captured"
    charged = [c for c in _calls(event_log.read(state.match_id), "attack") if c["agent"] == "alice"]
    assert len(charged) == len(alice_tries)
    assert charged[0].get("refused") is True
    assert "refused" not in charged[1]
    assert replay(state, event_log.read(state.match_id)).consistent


def test_refused_gate_probe_is_inconclusive(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    gate = Refuse("attack")
    state = make_engine({"alice": FakeProvider(), "bob": FakeProvider(), "gate": gate}, metrics=metrics).run(
        new_state(rounds=1, challenges_per_round=1, difficulty_gate=True)
    )
    for ch in state.challenges.values():
        assert state.validations[ch.id].valid
        assert not ch.probe_solved
        assert not any(err.startswith("too easy") for err in state.build_errors.get(ch.id, []))
    assert metrics.counters.get("gate.inconclusive") == 2
    assert metrics.counters.get("gate.held", 0) == 0
    assert metrics.counters.get("gate.solved", 0) == 0
    gates = [c for c in _calls(event_log.read(state.match_id), "attack") if c["agent"] == "gate"]
    assert len(gates) == 2
    assert all(c.get("refused") is True for c in gates)
    assert gate.refused == 2
    assert replay(state, event_log.read(state.match_id)).consistent
