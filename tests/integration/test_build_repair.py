"""Build repair loop against recorded real-model answers (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.domain import policy
from claude_duel.domain.models import ChallengeSpec, Rules
from claude_duel.ports import Completion, CompletionRequest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openai_build"


def recorded(name: str) -> str:
    return str(json.loads((FIXTURES / f"{name}.json").read_text())["content"])


class Scripted:
    """Replays recorded build answers in order; attacks go to the fake provider."""

    name = "scripted"

    def __init__(self, *builds: str) -> None:
        self.builds, self.prompts, self.fake = list(builds), [], FakeProvider()

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose != "build":
            return self.fake.complete(request)
        self.prompts.append(request.prompt)
        text = self.builds.pop(0) if len(self.builds) > 1 else self.builds[0]
        return Completion(text=text, input_tokens=100, output_tokens=50)


def test_invalid_answer_is_repaired_with_the_reason(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Scripted(recorded("nano_invents_flag"), recorded("nano_repaired"))
    state = make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    assert len(alice.prompts) == 2
    assert "FLAG is injected by the arena" in alice.prompts[1]
    mine = next(c for c in state.challenges.values() if c.author == "alice")
    assert state.validations[mine.id].valid
    assert [e.split(" (")[0] for e in state.build_errors[mine.id]] == [
        "FLAG is injected by the arena; never assign or define it"
    ]
    assert state.usage.calls >= 2  # the repair call is charged like any other


def test_repairs_exhausted_keeps_last_answer_invalid(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Scripted(recorded("mini_invents_flag"))
    rules = {"rounds": 1, "challenges_per_round": 1, "max_format_retries": 2}
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**rules))
    assert len(bob.prompts) == 3  # 1 + 2 repairs
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert mine.spec is not None
    v = state.validations[mine.id]
    assert not v.valid
    assert "FLAG is injected" in v.reason
    assert len(state.build_errors[mine.id]) == 3  # every rejection checkpointed


def test_sandbox_failure_is_fed_back(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    wrong = json.loads(recorded("nano_repaired"))
    wrong["reference_payload"] = "nope"
    alice = Scripted(json.dumps(wrong), recorded("nano_repaired"))
    make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    assert "reference payload does not reveal flag" in alice.prompts[1]


def test_prompt_states_the_policy_and_its_example_is_valid() -> None:
    prompt = agents.build_prompt("a", 1, 0, Rules(), None)
    for name in policy.BANNED_NAMES | policy.PURE_MODULES:
        assert name in prompt
    assert "sys.stdin" in prompt
    assert "__name__" in prompt
    assert "Never assign, define" in prompt
    example = ChallengeSpec.model_validate(agents.EXAMPLE_CHALLENGE)
    assert policy.check(example, Rules().max_source_bytes(1)) is None


def test_recorded_failures_are_caught_by_policy() -> None:
    for name in ("nano_invents_flag", "mini_invents_flag"):
        spec = agents.parse_challenge(recorded(name))
        assert (policy.check(spec, 4096) or "").startswith("FLAG is injected by the arena; never assign or define it")
    assert policy.check(agents.parse_challenge(recorded("nano_repaired")), 4096) is None


def test_plaintext_answer_is_rejected_and_explained(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Scripted(recorded("mini_plaintext"))
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(rounds=1, challenges_per_round=1))
    assert "appears in the source; derive it with logic" in bob.prompts[1]
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert not state.validations[mine.id].valid


class Probe:
    """Difficulty-gate stand-in that always answers the example maze's path."""

    name = "probe"

    def complete(self, request: CompletionRequest) -> Completion:
        return Completion(text='{"payload": "DDRR"}', input_tokens=80, output_tokens=8)


def test_solver_output_becomes_reference(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Scripted(recorded("mini_solver"))
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(rounds=1, challenges_per_round=1))
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[mine.id].valid
    assert mine.spec is not None
    assert mine.spec.reference_payload


def test_difficulty_gate_sends_easy_challenge_to_repair(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Scripted(json.dumps(agents.EXAMPLE_CHALLENGE), recorded("mini_solver"))
    engine = make_engine({"alice": alice, "bob": FakeProvider(), "gate": Probe()})
    state = engine.run(new_state(rounds=1, challenges_per_round=1, difficulty_gate=True))
    mine = next(c for c in state.challenges.values() if c.author == "alice")
    assert state.build_errors[mine.id][0].startswith("too easy")
    assert "too easy" in alice.prompts[1]
    assert state.validations[mine.id].valid  # the harder repair held against the probe


def test_difficulty_gate_off_by_default(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = Scripted(json.dumps(agents.EXAMPLE_CHALLENGE))
    make_engine({"alice": alice, "bob": FakeProvider()}).run(new_state(rounds=1, challenges_per_round=1))
    assert len(alice.prompts) == 1
