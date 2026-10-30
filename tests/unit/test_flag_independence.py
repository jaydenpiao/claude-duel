"""SV-1: a reference answer must not depend on FLAG; the solver never sees the real flag."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.ports import Completion, CompletionRequest

ONE = {"rounds": 1, "challenges_per_round": 1}
KEYED = "import sys\nif sys.stdin.read().strip() == FLAG[5:17]:\n    print(FLAG)\n"
LOCK = (
    "import sys\ns = sys.stdin.read().strip()\nif len(s) == 7 and s.isdigit() and int(s) % 97 == 3:\n    print(FLAG)\n"
)


class Builds:
    name = "builds"

    def __init__(self, **spec: object) -> None:
        self.text = json.dumps({"title": "t", "description": "d", "difficulty": 1, **spec})
        self.fake = FakeProvider()

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose != "build":
            return self.fake.complete(request)
        return Completion(text=self.text, input_tokens=10, output_tokens=10)


def _bob(make_engine, new_state, **spec):  # type: ignore[no-untyped-def]
    state = make_engine({"alice": FakeProvider(), "bob": Builds(**spec)}).run(new_state(**ONE))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    return state, ch


def test_solver_printing_a_flag_slice_is_rejected(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _bob(make_engine, new_state, source=KEYED, reference_payload="", solver="print(FLAG[5:17])\n")
    assert not state.validations[ch.id].valid


def test_flag_independent_solver_still_valid(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _bob(
        make_engine, new_state, source=LOCK, reference_payload="", solver="print(str(97 * 10000 + 3).zfill(7))\n"
    )
    assert state.validations[ch.id].valid, state.build_errors.get(ch.id)


def test_plain_reference_still_valid_under_two_flags(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _bob(make_engine, new_state, source=LOCK, reference_payload="0000003")
    assert state.validations[ch.id].valid
