"""The check challenge form (domain/contract.py) and specific repair hints."""

from __future__ import annotations

import json

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.app.engine import SOLVER_TIME_FACTOR
from claude_duel.domain import contract
from claude_duel.domain.models import Rules
from claude_duel.ports import Completion, CompletionRequest

CHECKED = {"rounds": 1, "challenges_per_round": 1, "challenge_form": "check"}


class Builds:
    """Answers builds in order (the last repeats); attacks go to the fake provider."""

    name = "builds"

    def __init__(self, *specs: dict[str, object]) -> None:
        self.texts, self.prompts, self.fake = [json.dumps(s) for s in specs], [], FakeProvider()

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose != "build":
            return self.fake.complete(request)
        self.prompts.append(request.prompt)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return Completion(text=text, input_tokens=10, output_tokens=10)


class TestCompose:
    def test_wraps_check_in_arena_io(self) -> None:
        source, problem = contract.compose("def check(s):\n    return s == 'x' * 5\n")
        assert problem is None
        assert source.endswith(contract.WRAPPER)
        assert contract.compose(source) == (source, None)  # an echoed wrapper is not doubled

    @pytest.mark.parametrize(
        ("source", "reason"),
        [
            ("def check(s):\n    return FLAG\n", "do not use FLAG"),
            ("import sys\ndef check(s):\n    return True\n", "do not use sys"),
            ("def solve(s):\n    return True\n", "exactly one top-level function check(s)"),
            ("def check(a, b):\n    return True\n", "exactly one top-level function check(s)"),
            ("def check(s)\n", "syntax error"),
        ],
    )
    def test_rejects(self, source: str, reason: str) -> None:
        assert reason in (contract.compose(source)[1] or "")

    def test_solver_follows_definitions(self) -> None:
        composed, _ = contract.compose("def check(s):\n    return s == 'abcd'\n")
        program = contract.solver_program(composed, "print(1)\n")
        assert contract.WRAPPER not in program
        assert program.endswith("print(1)\n")


@pytest.mark.parametrize("example", agents.CHECK_EXAMPLES, ids=lambda e: str(e["title"]))
def test_every_prompt_example_is_valid(make_engine, new_state, example) -> None:  # type: ignore[no-untyped-def]
    bob = Builds(example)
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**CHECKED))
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[mine.id].valid, state.build_errors
    assert mine.spec is not None
    assert mine.spec.reference_payload  # derived by the solver, which may call check()


def test_io_misuse_is_repaired_with_the_reason(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bad = {**agents.CHECK_EXAMPLES[0], "source": "import sys\nprint(FLAG)\n"}
    bob = Builds(bad, agents.CHECK_EXAMPLES[0])
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**CHECKED))
    assert "exactly one top-level function check(s)" in bob.prompts[1]
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[mine.id].valid


def test_solver_program_is_policy_checked(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Builds({**agents.CHECK_EXAMPLES[0], "solver": "import os\nprint(1)\n"})
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**CHECKED, max_format_retries=0))
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.build_errors[mine.id][0].startswith("solver: import not allowed: os")


def test_check_prompt_states_the_contract() -> None:
    prompt = agents.build_prompt("a", 1, 0, Rules(challenge_form="check"), None)
    assert "FLAG is already a global" in prompt
    assert "def check" in prompt
    assert "Minimal valid example" not in prompt
    assert "FLAG is already a global" not in agents.build_prompt("a", 1, 0, Rules(), None)


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        ("solver failed (exit 0); it printed ''", "Design backwards"),
        ("solver failed (exit 1); it printed 'error: NameError at line 9'", "never defined"),
        ("reference payload does not reveal flag; it printed nothing", "add a solver"),
        ("trivial: empty input reveals flag", "length test"),
        ("reference_payload must be at least 4 characters", "at least 4"),
        ("banned attribute: __dict__", "FLAG is already a global"),
        ("solver: import not allowed: random", "recurrence"),
        ("solver: banned name: exit (use sys.exit())", "Do not call exit()"),
        ("solver failed (exit -9); it printed 'a'", "only the one accepting input"),
        ("solver: import not allowed: collections", "lists and dicts"),
        ("the accepting input appears in the source as shifted or XORed char codes", "test properties"),
    ],
)
def test_repair_hints(error: str, hint: str) -> None:
    assert hint in agents.repair_hint(error)
    assert hint in agents.build_prompt("a", 1, 0, Rules(), error)


def test_unknown_error_has_no_hint() -> None:
    assert agents.repair_hint("something else") == ""


def test_build_answer_defaults_optional_fields() -> None:
    spec = agents.parse_challenge(json.dumps({"title": "t", "description": "d", "source": "x = 1\n"}))
    assert (spec.reference_payload, spec.difficulty) == ("", 2)


def test_solver_gets_more_time_and_a_clear_failure(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[str, float]] = []
    quiet = {**agents.CHECK_EXAMPLES[0], "solver": "x = 1\n"}
    engine = make_engine({"alice": FakeProvider(), "bob": Builds(quiet)})
    inner = engine._sandbox

    class Spy:
        def run(self, source, stdin, flag, limits):  # type: ignore[no-untyped-def]
            seen.append((source[-6:], limits.cpu_seconds))
            return inner.run(source, stdin, flag, limits)

    engine._sandbox = Spy()
    state = engine.run(new_state(**CHECKED, max_format_retries=0))
    assert ("x = 1\n", SOLVER_TIME_FACTOR * state.limits.cpu_seconds) in seen
    mine = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[mine.id].reason == "no reference input: the solver never printed one"
