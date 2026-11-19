"""A check that hangs on wrong input is rejected as too slow, including same-shape junk."""

from __future__ import annotations

import time

from claude_duel.app.engine import VALIDATION_TIME_FACTOR
from tests.helpers import hanging_source, run_bob


def test_a_check_that_hangs_on_wrong_input_is_rejected_fast(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    t = time.monotonic()
    state, ch = run_bob(make_engine, new_state, source=hanging_source(), reference_payload="0000003")
    took = time.monotonic() - t
    assert not state.validations[ch.id].valid
    assert any("timed out" in e for e in state.build_errors.get(ch.id, [])), state.build_errors
    # empty and junk probes share the budget; reject on the first hang
    assert took < (VALIDATION_TIME_FACTOR + 1) * state.limits.wall_seconds + 2


def test_hang_only_on_same_shape_is_too_slow(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = run_bob(
        make_engine, new_state, source=hanging_source(same_shape_only=True), reference_payload="0000003"
    )
    v = state.validations[ch.id]
    assert not v.valid
    assert "too slow" in (v.reason or "")
