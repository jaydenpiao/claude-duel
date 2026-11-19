"""SV-5: with attacker_solver off, an author's solver must fit the 1x time an attacker run gets."""

from __future__ import annotations

import time

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.ports import RunResult, SandboxLimits
from tests.helpers import ONE, Builds
from tests.unit.test_flag_independence import LOCK

SOLVER = "print(str(97 * 10000 + 3).zfill(7))\n"


class SlowSolver:
    """Solver runs (the only ones with stretched limits) take ~2x the wall plus grace."""

    def __init__(self, inner: SubprocessSandbox, base: SandboxLimits) -> None:
        self.inner, self.base = inner, base

    def run(self, source: str, stdin: str, flag: str, limits: SandboxLimits) -> RunResult:
        if limits.wall_seconds > self.base.wall_seconds:
            time.sleep(2 * self.base.wall_seconds + 0.6)
        return self.inner.run(source, stdin, flag, limits)


def _solve(make_engine, new_state, **rules):  # type: ignore[no-untyped-def]
    engine = make_engine({"alice": FakeProvider(), "bob": Builds(source=LOCK, reference_payload="", solver=SOLVER)})
    state = new_state(**ONE, **rules)
    state.limits = SandboxLimits(wall_seconds=0.5)
    engine._sandbox = SlowSolver(engine._sandbox, state.limits)
    state = engine.run(state)
    return state, next(c for c in state.challenges.values() if c.author == "bob")


def test_slow_solver_rejected_without_attacker_solver(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _solve(make_engine, new_state)
    assert not state.validations[ch.id].valid
    assert any("solver too slow" in e for e in state.build_errors.get(ch.id, []))


def test_fast_solver_still_valid(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine({"alice": FakeProvider(), "bob": Builds(source=LOCK, reference_payload="", solver=SOLVER)})
    state = engine.run(new_state(**ONE))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[ch.id].valid
