"""S11-1: a check that hangs only on wrong inputs shaped like the answer is rejected as too slow."""

from __future__ import annotations

from tests.helpers import run_bob

SRC = (
    "import sys\n"
    "s = sys.stdin.read().strip()\n"
    "if s.isdigit() and int(s) * 7 == 21 and len(s) == 7:\n    print(FLAG)\n"
    "elif s.isdigit() and len(s) == 7:\n    n = 0\n    for i in range(10**10):\n        n += i\n"
    "else:\n    print('no')\n"
)


def test_hang_only_on_same_shape_is_too_slow(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = run_bob(make_engine, new_state, source=SRC, reference_payload="0000003")
    v = state.validations[ch.id]
    assert not v.valid
    assert "too slow" in (v.reason or "")
