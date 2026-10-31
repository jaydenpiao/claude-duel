"""Design rec R1: a solver check() budget (upper bound) and a seeded density band (lower bound)."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.domain import solvability
from claude_duel.domain.models import SolvabilityRule
from claude_duel.domain.modes import preset
from tests.helpers import ONE, Builds

SPARSE = "def check(s):\n    return len(s) == 5 and s.isdigit() and int(s) == 28783\n"
SEARCH = "for n in range(100000):\n    if check(str(n).zfill(5)):\n        print(str(n).zfill(5))\n        break\n"
DENSE = "def check(s):\n    return len(s) == 6 and s.islower() and sum(ord(c) for c in s) % 16 == 5\n"


def _run(make_engine, new_state, source, solver, **band):  # type: ignore[no-untyped-def]
    engine = make_engine({"alice": FakeProvider(), "bob": Builds(source=source, reference_payload="", solver=solver)})
    rule = SolvabilityRule(enabled=True, **band)
    state = engine.run(new_state(**ONE, challenge_form="check", solvability=rule))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    return state, ch


def test_brute_force_solver_goes_back_to_repair(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _run(make_engine, new_state, SPARSE, SEARCH)
    assert not state.validations[ch.id].valid
    assert any("called check() 28,784 times" in e for e in state.build_errors.get(ch.id, []))


def test_bigger_budget_accepts_and_logs_the_count(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _run(make_engine, new_state, SPARSE, SEARCH, max_solver_checks=200_000)
    v = state.validations[ch.id]
    assert v.valid, v.reason
    assert v.solver_checks == 28_784
    assert v.density_ppm == 0
    assert v.log2_space == 16.61


def test_constructive_solver_makes_no_calls(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _run(make_engine, new_state, SPARSE, "print('28783')\n")
    v = state.validations[ch.id]
    assert v.valid, v.reason
    assert v.solver_checks == 0


def test_guessable_check_goes_back_to_repair(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _run(make_engine, new_state, DENSE, "print('abcdef')\n")
    assert not state.validations[ch.id].valid
    assert any("too easy to guess" in e for e in state.build_errors.get(ch.id, [])), state.build_errors


def test_off_by_default_and_on_in_checked() -> None:
    assert not preset("classic").solvability.enabled
    assert preset("checked").solvability.enabled
    assert SolvabilityRule().solver_cap(attacker_solver=False) == 2_000
    assert SolvabilityRule().solver_cap(attacker_solver=True) == 200_000


def test_samples_are_seeded_and_shaped() -> None:
    a = solvability.samples("seed", "c1", "12345", 50)
    assert a == solvability.samples("seed", "c1", "12345", 50)
    assert a != solvability.samples("seed", "c2", "12345", 50)
    assert all(len(s) == 5 and s.isdigit() for s in a)
    assert all(s.islower() and len(s) == 70 for s in solvability.samples("x", "c", "a" * 70, 3))


def test_count_line_needs_the_exact_tag() -> None:
    assert solvability.split_count("42\nNabc:7\n", "Nabc") == ("42", 7)
    assert solvability.split_count("42\nNxyz:7\n", "Nabc") == ("42\nNxyz:7\n", None)
    assert solvability.split_count("42\nNabc:7x\n", "Nabc")[1] is None
    assert solvability.nonce("s", "c", "solver") != solvability.nonce("s", "c", "density")
