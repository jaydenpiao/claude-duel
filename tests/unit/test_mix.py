"""R5 follow-up: the arena's published mix(x) is available to check-form code and its solver."""

from __future__ import annotations

from claude_duel.domain import contract

SRC = "def check(s):\n    return s.isdigit() and mix(int(s)) % 7 == 3\n"


def _mix(x: int) -> int:
    env: dict[str, object] = {}
    exec(contract.MIX_DEF, env)
    return env["mix"](x)  # type: ignore[operator, no-any-return]


def test_mix_is_a_stable_32_bit_formula() -> None:
    assert _mix(0) == 0
    assert _mix(1) == _mix(1 + 2**32)  # masked to 32 bits
    assert 0 <= _mix(123456789) < 2**32
    assert len({_mix(i) for i in range(1000)}) == 1000  # a bijection on its domain


def test_composed_challenge_and_solver_can_call_mix() -> None:
    source, problem = contract.compose(SRC)
    assert problem is None
    assert contract.definitions(source).rstrip() == SRC.rstrip()  # stripped back exactly
    env: dict[str, object] = {}
    exec(contract.solver_program(source, "pass"), env)
    check = env["check"]
    hits = [n for n in range(50) if check(str(n))]  # type: ignore[operator]
    assert hits
    assert all(_mix(n) % 7 == 3 for n in hits)
    assert "mix" in contract.with_feedback(source)


def test_mix_cannot_be_rebound() -> None:
    _, problem = contract.compose(SRC + "\ndef mix(x):\n    return 0\n")
    assert problem
    assert "mix" in problem


def test_old_wrapper_still_strips() -> None:
    old = SRC.rstrip() + contract._LEGACY_WRAPPERS[0]
    assert contract.definitions(old) == SRC.rstrip()
