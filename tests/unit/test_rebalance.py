"""Attacker-side rebalancing for the check form (#95): early bonus tries and a retry hint."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.domain.modes import preset
from claude_duel.ports import Completion, CompletionRequest


def test_bonus_tries_shrink_each_round(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state(rounds=4, max_attempts=3, early_bonus_attempts=2)
    seen = []
    for r in (0, 1, 2, 3, 4):
        state.round = r
        seen.append(state.max_attempts("alice"))
    assert seen == [5, 5, 4, 3, 3]


def test_checked_preset_rebalances() -> None:
    rules = preset("checked")
    assert (rules.early_bonus_attempts, rules.retry_hint) == (2, True)


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.fake, self.prompts = FakeProvider(skill=1), []

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose == "attack":
            self.prompts.append(request.prompt)
        return self.fake.complete(request)


def _attack_prompts(make_engine, new_state, **rules):  # type: ignore[no-untyped-def]
    alice, bob = Recorder(), Recorder()
    make_engine({"alice": alice, "bob": bob}).run(new_state(rounds=1, max_attempts=3, **rules))
    return alice.prompts + bob.prompts


def test_retry_hint_only_after_a_failed_try(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    prompts = _attack_prompts(make_engine, new_state, retry_hint=True)
    firsts = [p for p in prompts if "ATTEMPT: 1\n" in p]
    laters = [p for p in prompts if "ATTEMPT: 1\n" not in p]
    assert firsts
    assert laters
    assert not any(agents.RETRY_HINT in p for p in firsts)
    assert all(agents.RETRY_HINT in p for p in laters)


def test_retry_hint_off_by_default(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    assert not any(agents.RETRY_HINT in p for p in _attack_prompts(make_engine, new_state))
