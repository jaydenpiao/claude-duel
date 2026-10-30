"""SV-4: no hash(), fixed str hashing in the sandbox, and a reference that must print the same on every run."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.domain import policy
from claude_duel.ports import RunResult, SandboxLimits
from tests.unit.test_flag_independence import LOCK, ONE, Builds

SET_ORDER = "print(''.join({'alpha', 'beta', 'gamma', 'delta', 'eps'}))\n"


def test_hash_builtin_is_rejected() -> None:
    assert policy.check_source("import sys\nif hash(sys.stdin.read()) % 2:\n    print(FLAG)\n", 4096)
    assert "hash" not in policy.SAFE_BUILTINS


def test_set_order_is_stable_across_processes() -> None:
    box = SubprocessSandbox(lambda _: True)
    outs = {box.run(SET_ORDER, "", "DUEL{x}", SandboxLimits()).stdout for _ in range(4)}
    assert len(outs) == 1


class Flaky:
    """Every other run of the reference payload prints something extra."""

    def __init__(self, inner: SubprocessSandbox, payload: str) -> None:
        self.inner, self.payload, self.n = inner, payload, 0

    def run(self, source: str, stdin: str, flag: str, limits: SandboxLimits) -> RunResult:
        out = self.inner.run(source, stdin, flag, limits)
        if stdin == self.payload:
            self.n += 1
            if self.n % 2 == 0:
                return out.model_copy(update={"stdout": out.stdout + "!"})
        return out


def test_reference_that_flips_between_runs_is_invalid(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine({"alice": FakeProvider(), "bob": Builds(source=LOCK, reference_payload="0000003")})
    engine._sandbox = Flaky(engine._sandbox, "0000003")
    state = engine.run(new_state(**ONE))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    assert not state.validations[ch.id].valid
    assert any("nondeterministic" in e for e in state.build_errors.get(ch.id, []))
