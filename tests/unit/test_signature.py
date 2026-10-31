"""R2 lock-family signatures, the R5 structural fingerprint and their use in binding and repeats."""

import pytest

from claude_duel.domain import memory, signature, variety
from claude_duel.domain.models import Challenge, MatchState
from tests.unit.test_family_binding import _spec, _state

FSM = """
T = {(0, 'a'): 1, (1, 'b'): 0}
def check(s):
    state = 0
    for c in s:
        state = T.get((state, c), -1)
    return state == 0 and len(s) == 4
"""
FSM_RENAMED = FSM.replace("state", "q").replace("T", "M").replace("4", "6")
DIGITS = """
def check(s):
    return len(s) == 4 and s.isdigit() and sum(int(c) for c in s) == 20 and int(s[0]) > int(s[3])
"""
BITS = """
def check(s):
    h = 7
    for c in s:
        h = ((h << 3) ^ ord(c)) & 255
    return h == 99 and (h >> 1) > 2
"""
PLAIN = """
def check(s):
    return s == s[::-1] and len(s) == 5 and s.count('a') == 2
"""


@pytest.mark.parametrize(
    ("source", "want"),
    [(FSM, ["state-machine"]), (DIGITS, ["digit-lock"]), (BITS, ["bitwise"]), (PLAIN, []), ("def (:", [])],
)
def test_signatures(source: str, want: list[str]) -> None:
    assert signature.signatures(source) == want


def test_fingerprint_ignores_names_and_constants() -> None:
    assert signature.tokens(FSM) == signature.tokens(FSM_RENAMED)
    assert signature.fingerprint(FSM) == signature.fingerprint(FSM_RENAMED) != signature.fingerprint(PLAIN)
    assert signature.tokens("def (:") == ()


def test_technique_is_structural() -> None:
    assert memory.technique(FSM).startswith("state-machine#")
    assert memory.technique(FSM) == memory.technique(FSM_RENAMED)
    assert memory.technique(PLAIN).startswith("other#")


@pytest.mark.parametrize("axis", sorted(variety.AXIS_EXAMPLES))
def test_axis_examples_match_their_axis(axis: str) -> None:
    sigs = signature.signatures(str(variety.AXIS_EXAMPLES[axis]["source"]))
    if axis != variety.LOCK_AXIS:
        assert sigs == []


def _slot(lock: bool) -> tuple[MatchState, int]:
    for seed in range(200):
        state = _state(f"s{seed}")
        state.round = 1
        for index in range(2):
            axis, _ = variety.state_family(state, "haiku", index)
            if (axis == variety.LOCK_AXIS) == lock:
                return state, index
    raise AssertionError("no slot found")


def test_a_lock_structure_outside_the_lock_axis_is_sent_back() -> None:
    state, index = _slot(lock=False)
    spec = _spec("").model_copy(update={"source": FSM})
    reason = variety.family_reason(state, "haiku", index, spec)
    assert reason is not None
    assert "lock-puzzle structure [state-machine" in reason
    assert variety.family_reason(state, "haiku", index, spec.model_copy(update={"source": PLAIN})) is None


def test_lock_structures_are_fine_on_the_lock_axis() -> None:
    state, index = _slot(lock=True)
    assert variety.family_reason(state, "haiku", index, _spec("").model_copy(update={"source": DIGITS})) is None


def test_a_renamed_copy_is_a_repeat() -> None:
    state = _state()
    old = _spec("").model_copy(update={"title": "old", "source": FSM})
    state.challenges["c1"] = Challenge(id="c1", author="haiku", round=1, spec=old)
    new = old.model_copy(update={"title": "new", "source": FSM_RENAMED})
    assert "same as an earlier one" in (variety.repeat_reason(state, "haiku", new) or "")
    assert variety.repeat_reason(state, "haiku", new.model_copy(update={"source": PLAIN})) is None
