"""A refused turn is a forfeit: scored, replayable, and resumable without a second paid call (ADR 0016)."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.engine import REFUSED
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.models import Stage
from claude_duel.domain.scoring import defense_award
from claude_duel.ports import Completion, CompletionRequest
from claude_duel.presentation.card import card_data, card_svg


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


def test_refused_attack_ends_the_turn_and_resumes(make_engine, new_state, store, event_log) -> None:  # type: ignore[no-untyped-def]
    alice = Refuse("attack")
    engine = make_engine({"alice": alice, "bob": FakeProvider()})
    state = engine.run(new_state(rounds=1, challenges_per_round=1))
    mine = next(a for a in state.attacks.values() if a.attacker == "alice")
    assert not mine.solved
    assert mine.reason == REFUSED
    assert mine.attempts == 1
    attempts = [e for e in event_log.read(state.match_id) if e.type == ev.ATTACK_ATTEMPTED]
    alice_tries = [e for e in attempts if str(e.data["key"]).endswith(":alice")]
    assert len(alice_tries) == 1
    assert alice_tries[0].data["attempt"]["output"] == REFUSED
    attacks = _calls(event_log.read(state.match_id), "attack")
    assert all(c.get("refused") is True for c in attacks if c["agent"] == "alice")
    assert all("refused" not in c for c in attacks if c["agent"] != "alice")
    # a valid defense still pays the author (forfeit by the attacker, not a weak try that keeps going)
    bob = next(c for c in state.challenges.values() if c.author == "bob")
    assert bob.spec is not None
    assert state.validations[bob.id].valid
    assert state.scores["bob"] >= defense_award(bob.spec, bob.round, state.rules.scoring)
    report = replay(state, event_log.read(state.match_id))
    assert report.consistent
    assert report.refusals == 1
    again = engine.run(store.load(state.match_id))
    assert again.usage.calls == state.usage.calls
    assert alice.refused == 1
