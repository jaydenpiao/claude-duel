"""R3-3: check-form authors cannot reach FLAG by rebinding print (or replacing check)."""

from __future__ import annotations

import pytest

from claude_duel.domain import contract, policy

CHECK = "def check(s):\n    return True\n"


@pytest.mark.parametrize(
    "source",
    [
        "p = print\ndef print(x):\n    p('R:' + x[::-1])\n" + CHECK,  # the round-3 repro
        "print = len\n" + CHECK,
        "from math import sqrt as print\n" + CHECK,
        "class print:\n    pass\n" + CHECK,
        "def f(print=None):\n    return print\n" + CHECK,
        "def g():\n    global print\n" + CHECK,
        "def deco(f):\n    return f\n@deco\n" + CHECK,
        CHECK + "check = lambda s: True\n",
        "for print in range(2):\n    pass\n" + CHECK,
    ],
)
def test_rebinding_print_or_check_is_rejected(source: str) -> None:
    _, problem = contract.compose(source)
    assert problem is not None
    assert "do not rebind" in problem


def test_wrapper_never_calls_print_and_still_composes() -> None:
    assert "print" not in contract.WRAPPER
    composed, problem = contract.compose("def check(s):\n    return s == 'x' * 5\n")
    assert problem is None
    assert policy.check_source(composed, 4096) is None
    legacy = (
        "def check(s):\n    return True\n\n\nimport sys\nif check(sys.stdin.read().strip()) is True:\n    print(FLAG)\n"
    )
    assert contract.definitions(legacy) == CHECK  # older logged sources still strip


def test_plain_helpers_still_pass() -> None:
    source = "def helper(x):\n    return x * 2\n\ndef check(s):\n    v = helper(len(s))\n    return v == 10\n"
    assert contract.compose(source)[1] is None
