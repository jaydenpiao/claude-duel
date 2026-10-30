"""SV-1: a reference answer must not depend on FLAG; the solver never sees the real flag."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.domain.flags import derive_flag, flag_of, shares_fragment
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


def test_fragment_window_detected() -> None:
    flag = derive_flag("s", "c")
    inner = flag[5:-1]
    assert shares_fragment(f"x{inner[3:11]}y", flag)
    assert shares_fragment(inner[20:28].upper(), flag)
    assert not shares_fragment(inner[3:10], flag)  # 7 chars is below the window


def test_no_flag_fragment_reaches_the_log(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state, ch = _bob(make_engine, new_state, source=KEYED, reference_payload="", solver="print(FLAG[5:17])\n")
    body = "".join(e.model_dump_json() for e in event_log.read(state.match_id))
    for epoch in range(len(state.build_errors.get(ch.id, [])) + 1):
        assert not shares_fragment(body.replace("DUEL{<redacted>}", ""), flag_of(state.secret, ch.id, epoch))
