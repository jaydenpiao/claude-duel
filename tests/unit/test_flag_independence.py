"""SV-1: a reference answer must not depend on FLAG; the solver never sees the real flag."""

from __future__ import annotations

import pytest

from claude_duel.domain.flags import derive_flag, flag_of, shares_fragment
from tests.helpers import hanging_source, run_bob

KEYED = "import sys\nif sys.stdin.read().strip() == FLAG[5:17]:\n    print(FLAG)\n"
LOCK = (
    "import sys\ns = sys.stdin.read().strip()\nif len(s) == 7 and s.isdigit() and int(s) % 97 == 3:\n    print(FLAG)\n"
)


def test_solver_printing_a_flag_slice_is_rejected(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = run_bob(make_engine, new_state, source=KEYED, reference_payload="", solver="print(FLAG[5:17])\n")
    assert not state.validations[ch.id].valid


def test_flag_independent_solver_still_valid(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = run_bob(
        make_engine, new_state, source=LOCK, reference_payload="", solver="print(str(97 * 10000 + 3).zfill(7))\n"
    )
    assert state.validations[ch.id].valid, state.build_errors.get(ch.id)


def test_plain_reference_still_valid_under_two_flags(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = run_bob(make_engine, new_state, source=LOCK, reference_payload="0000003")
    assert state.validations[ch.id].valid


def test_fragment_window_detected() -> None:
    flag = derive_flag("s", "c")
    inner = flag[5:-1]
    assert shares_fragment(f"x{inner[3:11]}y", flag)
    assert shares_fragment(inner[20:28].upper(), flag)
    assert not shares_fragment(inner[3:10], flag)  # 7 chars is below the window


def test_no_flag_fragment_reaches_the_log(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state, ch = run_bob(make_engine, new_state, source=KEYED, reference_payload="", solver="print(FLAG[5:17])\n")
    body = "".join(e.model_dump_json() for e in event_log.read(state.match_id))
    for epoch in range(len(state.build_errors.get(ch.id, [])) + 1):
        assert not shares_fragment(body.replace("DUEL{<redacted>}", ""), flag_of(state.secret, ch.id, epoch))


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nif sys.stdin.read():\n    print(FLAG)\n",
        "import sys\nif len(sys.stdin.read().strip()) > 0:\n    print(FLAG)\n",
        "import sys\nif len(sys.stdin.read().strip()) == 7:\n    print(FLAG)\n",
    ],
)
def test_degenerate_acceptance_is_rejected(make_engine, new_state, source: str) -> None:  # type: ignore[no-untyped-def]
    state, ch = run_bob(make_engine, new_state, source=source, reference_payload="0000003")
    assert not state.validations[ch.id].valid
    errors = state.build_errors.get(ch.id, [])
    reason = state.validations[ch.id].reason or ""
    assert any("junk input" in e for e in errors) or "junk input" in reason


def test_validation_over_its_time_budget_is_rejected(make_engine, new_state, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("claude_duel.app.engine.VALIDATION_TIME_FACTOR", -1)
    state, ch = run_bob(make_engine, new_state, source=hanging_source(), reference_payload="0000003")
    assert not state.validations[ch.id].valid
    assert any("time budget" in e for e in state.build_errors.get(ch.id, [])), state.build_errors
