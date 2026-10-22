"""Attack-time feedback for the check form (#100): why check() said no, never its values."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from claude_duel.domain import contract
from claude_duel.domain.models import CAPTURED
from claude_duel.domain.modes import preset
from claude_duel.ports import Completion, CompletionRequest

CHECK = "def check(s):\n    n = int(s)\n    return n * 7 == 294294\n"
SPEC = {"title": "Seven", "description": "d", "source": CHECK, "reference_payload": "42042", "difficulty": 1}
TRIES = ["abcde", "11111", "42042"]


def _run(source: str, stdin: str) -> str:
    program = f"FLAG = 'DUEL{{x}}'\n{source}"
    return subprocess.run(
        [sys.executable, "-c", program], check=False, input=stdin, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize(
    ("stdin", "out"),
    [("11111", "check returned False"), ("42042", "DUEL{x}")],
)
def test_feedback_wrapper(stdin: str, out: str) -> None:
    source, problem = contract.compose(CHECK)
    assert problem is None
    assert _run(contract.with_feedback(source), stdin) == out
    assert _run(source, stdin) == ("DUEL{x}" if out == "DUEL{x}" else "")


def test_non_bool_reports_type_and_other_forms_pass_through() -> None:
    source, _ = contract.compose("def check(s):\n    return len(s)\n")
    assert _run(contract.with_feedback(source), "abcdef") == "check returned a non-bool"
    assert contract.with_feedback("print(FLAG)") == "print(FLAG)"


def test_checked_preset_turns_it_on() -> None:
    assert preset("checked").retry_feedback is True
    assert preset("classic").retry_feedback is False


class Scripted:
    name = "scripted"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose == "build":
            return Completion(text=json.dumps(SPEC), input_tokens=10, output_tokens=10)
        self.prompts.append(request.prompt)
        payload = TRIES[min(len(self.prompts), len(TRIES)) - 1]
        return Completion(text=json.dumps({"payload": payload}), input_tokens=10, output_tokens=10)


@pytest.mark.parametrize("feedback", [True, False])
def test_multi_try_capture_sees_feedback(make_engine, new_state, feedback: bool) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Scripted(), Scripted()
    rules = {"rounds": 1, "challenges_per_round": 1, "challenge_form": "check", "max_attempts": 3}
    state = make_engine({"alice": alice, "bob": bob}).run(new_state(**rules, retry_feedback=feedback))
    attacks = list(state.attempts.values())
    assert attacks
    for history in attacks:
        assert [a.output for a in history][-1] == CAPTURED
        assert len(history) == len(TRIES)
    assert "error: ValueError" in alice.prompts[1]  # the runner already names exceptions
    assert ("check returned False" in alice.prompts[2]) is feedback
