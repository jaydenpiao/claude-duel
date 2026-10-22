"""R3-2: a failing solver's output never stores a flag, and each repair try runs under a fresh flag."""

from __future__ import annotations

import json
from pathlib import Path

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.replay import replay
from claude_duel.domain.flags import REDACTED, flag_of
from claude_duel.ports import Completion, CompletionRequest

ONE = {"rounds": 1, "challenges_per_round": 1}
GUARD = "import sys\nif sys.stdin.read().strip() == 'opensesame':\n    print(FLAG)\n"
LOCK = (
    "import sys\ns = sys.stdin.read().strip()\nif len(s) == 7 and s.isdigit() and int(s) % 97 == 3:\n    print(FLAG)\n"
)
LEAKY = "import sys\nprint(FLAG)\nprint(FLAG[::-1])\nprint(FLAG.encode().hex())\nsys.exit(1)\n"


class Script:
    """Answers builds from a list (the last answer repeats); attacks go to the fake provider."""

    name = "script"

    def __init__(self, *specs: dict[str, object]) -> None:
        self.texts = [json.dumps({"title": "t", "description": "d", "difficulty": 1, **s}) for s in specs]
        self.fake = FakeProvider()

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose != "build":
            return self.fake.complete(request)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return Completion(text=text, input_tokens=10, output_tokens=10)


def _all_flags(state, cid: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [flag_of(state.secret, cid, e) for e in range(len(state.build_errors.get(cid, [])) + 1)]


def test_failing_solver_output_is_redacted(make_engine, new_state, store_root: Path) -> None:  # type: ignore[no-untyped-def]
    bob = Script({"source": GUARD, "reference_payload": "", "solver": LEAKY})
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(**ONE))
    cid = next(c.id for c in state.challenges.values() if c.author == "bob")
    errors = state.build_errors[cid]
    assert errors
    assert all(e.startswith("solver failed (exit 1)") and REDACTED in e for e in errors)
    on_disk = "".join(p.read_text() for p in store_root.glob("*.json*"))
    for flag in _all_flags(state, cid):
        for variant in (flag, flag[::-1], flag.encode().hex()):
            assert variant not in on_disk


def test_each_repair_try_gets_a_fresh_flag(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    bad = {"source": GUARD, "reference_payload": "", "solver": LEAKY}
    good = {"source": LOCK, "reference_payload": "0000003"}
    state = make_engine({"alice": FakeProvider(), "bob": Script(bad, good)}).run(new_state(**ONE))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    assert ch.flag_epoch == 1, state.build_errors[ch.id]
    assert state.validations[ch.id].valid
    flags = _all_flags(state, ch.id)
    assert len(set(flags)) == len(flags) == 2
    assert flag_of(state.secret, ch.id, 0) == flag_of(state.secret, ch.id)  # epoch 0 = the original derivation
    assert replay(state, event_log.read(state.match_id)).consistent
