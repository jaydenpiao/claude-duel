"""Design rec R1: a solver check() budget (upper bound) and a seeded density band (lower bound)."""

from __future__ import annotations

import contextlib
import io

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.engine import MatchEngine
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
    assert any("31,662 check() calls" in e for e in state.build_errors.get(ch.id, []))


def test_bigger_budget_accepts_and_logs_the_count(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, ch = _run(make_engine, new_state, SPARSE, SEARCH, max_solver_checks=200_000)
    v = state.validations[ch.id]
    assert v.valid, v.reason
    assert v.solver_checks == 28_784 + 28_784 // solvability.WORK_PER_CHECK
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


def test_attacker_solver_shares_the_author_budget(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state()
    assert MatchEngine._attack_over_budget(state, 0) is None
    assert MatchEngine._attack_over_budget(state, 200_000) is None
    assert "over budget" in (MatchEngine._attack_over_budget(state, 200_001) or "")
    assert "exited early" in (MatchEngine._attack_over_budget(state, None) or "")


HELPER = SPARSE.replace("def check(s)", "def verify(s)") + "\n\ndef check(s):\n    return verify(s)\n"
VIA_HELPER = SEARCH.replace("check(", "verify(")
ALIAS = SPARSE + "\n\ng = check\n"
VIA_ALIAS = SEARCH.replace("check(", "g(")
CLOSURE = SPARSE + "\n\ndef get():\n    return lambda s: check(s)\n"


def test_solver_calling_a_helper_is_counted(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    assert solvability.uncounted_path(HELPER, VIA_HELPER) is None
    state, ch = _run(make_engine, new_state, HELPER, VIA_HELPER)
    assert not state.validations[ch.id].valid
    assert any("31,662 check() calls" in e for e in state.build_errors.get(ch.id, [])), state.build_errors
    state, ch = _run(make_engine, new_state, HELPER, VIA_HELPER, max_solver_checks=200_000)
    assert (
        state.validations[ch.id].solver_checks == 28_784 + 2_878
    )  # check() inside verify() is not counted twice; + loop ticks


def test_source_aliasing_check_is_rejected(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    assert "aliases check" in (solvability.uncounted_path(ALIAS, VIA_ALIAS) or "")
    state, ch = _run(make_engine, new_state, ALIAS, VIA_ALIAS)
    assert not state.validations[ch.id].valid
    assert any("aliases check" in e for e in state.build_errors.get(ch.id, [])), state.build_errors


def test_solver_may_not_alias_or_get_a_closure() -> None:
    assert "not use check" in (solvability.uncounted_path(SPARSE, "g = check\nprint(g('1'))\n") or "")
    assert "lambda" in (solvability.uncounted_path(CLOSURE, "f = get()\nprint(f('1'))\n") or "")


def test_solver_may_use_check_mix_and_its_own_imports() -> None:
    src = "import string\n\n\ndef check(s):\n    return mix(len(s)) > 0 and s in string.ascii_lowercase\n"
    assert solvability.uncounted_path(src, "import string\nprint(mix(1), check('a'), string.digits)\n") is None


FIRST = SPARSE + "\n\ndef first(xs):\n    for x in xs:\n        if x:\n            return x\n    return None\n"
VIA_FIRST = "print(first(str(n).zfill(5) if check(str(n).zfill(5)) else None for n in range(100000)))\n"
RUN = SPARSE + "\n\ndef run(f):\n    return f()\n"
VIA_RUN = "def solve():\n" + "".join("    " + line + "\n" for line in SEARCH.splitlines()) + "print(run(solve))\n"
PICK = SPARSE + "\n\ndef pick(xs, key):\n    for x in xs:\n        if key(x):\n            return x\n    return None\n"
BODY = "".join("        " + line + "\n" for line in SEARCH.splitlines())
VIA_CLOSURE = "def outer():\n    def inner():\n" + BODY + "    return inner\n\n\nprint(run(outer()))\n"
APPLY = SPARSE + "\n\ndef apply(d):\n    return d['f']()\n"
VIA_DICT = "def table():\n    def solve():\n" + BODY + "    return {'f': solve}\n\n\nprint(apply(table()))\n"
VIA_PICK = "print(pick([str(n).zfill(5) for n in range(100000)], lambda s: check(s)))\n"


def _counted_run(source: str, solver: str) -> tuple[str, int | None]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            exec(solvability.counted(source, solver, "Ntag"), {"__name__": "__main__"})
        except TypeError as e:
            return f"TypeError: {e}", None
    return solvability.split_count(out.getvalue(), "Ntag")


def test_lazy_or_callable_arguments_cannot_hide_a_brute_force() -> None:
    for source, solver in ((FIRST, VIA_FIRST), (RUN, VIA_RUN), (PICK, VIA_PICK), (RUN, VIA_CLOSURE), (APPLY, VIA_DICT)):
        assert solvability.uncounted_path(source, solver) is None
        out, n = _counted_run(source, solver)
        assert n is None, (solver, out)
        assert "plain data" in out, (solver, out)


def test_plain_data_arguments_still_work_and_check_counts_at_any_depth() -> None:
    src = SPARSE + "\n\ndef scan(xs):\n    return [x for x in xs if check(x)]\n"
    out, n = _counted_run(src, "print(scan([str(n).zfill(5) for n in range(28780, 28790)]))\n")
    assert out.strip() == "['28783']"
    assert n == 10 + 10 // solvability.WORK_PER_CHECK  # 10 checks + 10 comprehension ticks
    assert _counted_run(HELPER, "print(verify('28783'), {'a': (1, b'x', None)} and 1)\n")[1] == 1


MIXED = "def check(s):\n    return len(s) == 5 and s.isdigit() and mix(int(s)) % 1000003 == mix(28783) % 1000003\n"
INLINED = (
    "t = mix(28783) % 1000003\nfor n in range(100000):\n"
    "    if mix(n) % 1000003 == t:\n        print(str(n).zfill(5))\n        break\n"
)
INLINED_MIX = (
    "def m(x):\n    x = int(x) & 0xFFFFFFFF\n    return x ^ (x >> 16)\n\n"
    "print([n for n in range(100000) if m(n) == 7][:1])\n"
)
LOOPS = "f = lambda x: x\nprint(sum(f(i) for i in range(50)))\nk = 0\nwhile k < 30:\n    k += 1\n"


def test_inlining_check_or_mix_still_counts_as_work(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    """A solver that copies check's logic (calling mix itself, or its own copy) never calls check."""
    for source, solver in ((MIXED, INLINED), (SPARSE, INLINED_MIX)):
        assert solvability.uncounted_path(source, solver) is None
        state, ch = _run(make_engine, new_state, source, solver)
        assert not state.validations[ch.id].valid
        assert any("brute-force" in e for e in state.build_errors.get(ch.id, [])), state.build_errors


def test_work_ticks_cover_loops_comprehensions_and_lambdas() -> None:
    out, n = _counted_run(SPARSE, LOOPS)
    assert out.strip() == "1225"
    assert n == (50 + 50 + 30) // solvability.WORK_PER_CHECK
