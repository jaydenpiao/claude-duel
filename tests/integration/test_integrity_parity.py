"""Integrity control and attacker solver parity (#150)."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import agents
from claude_duel.app.engine import CONTROL_SOURCE, MALFORMED, MatchEngine
from claude_duel.domain.models import SandboxLimits, SolvabilityRule
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest, RunResult, Sandbox
from tests.helpers import Builds
from tests.unit.test_solvability import SPARSE

MARK = "# attacker-solver"


class ViaSolver:
    """Sends the fake provider's attack payload through a solver, with a wrong literal payload."""

    name = "via-solver"

    def __init__(self, solver: str | None = None) -> None:
        self.fake, self.solver, self.requests = FakeProvider(skill=8), solver, list[CompletionRequest]()

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        done = self.fake.complete(request)
        if request.purpose != "attack":
            return done
        payload = json.loads(done.text.strip("`json\n"))["payload"]
        solver = self.solver if self.solver is not None else f"{MARK}\nprint({payload!r})"
        return Completion(text=json.dumps({"payload": "nope", "solver": solver}), input_tokens=5, output_tokens=5)


class Spy:
    """Records (source, flag) of every run; ``leak`` appends the flag to the control's stdout, standing in for a
    harness that hands the flag to any program (leaking everywhere would also fail validation, so no attack runs)."""

    def __init__(self, inner: Sandbox, leak: bool = False) -> None:
        self.inner, self.leak, self.runs = inner, leak, list[tuple[str, str]]()

    def run(self, source: str, stdin: str, flag: str, limits: SandboxLimits) -> RunResult:
        self.runs.append((source, flag))
        out = self.inner.run(source, stdin, flag, limits)
        return out.model_copy(update={"stdout": out.stdout + flag}) if self.leak and source == CONTROL_SOURCE else out


def _spy(engine, leak: bool = False) -> Spy:  # type: ignore[no-untyped-def]
    engine._sandbox = spy = Spy(engine._sandbox, leak)
    return spy


def _bob_attacks(state):  # type: ignore[no-untyped-def]
    return [a for a in state.attacks.values() if a.attacker == "bob"]


def test_solver_stdout_is_the_payload_and_never_sees_the_flag(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = ViaSolver()
    engine = make_engine({"alice": FakeProvider(), "bob": bob})
    spy = _spy(engine)
    state = engine.run(new_state(rounds=1, challenges_per_round=1, attacker_solver=True))
    (attack,) = _bob_attacks(state)
    assert attack.solved
    real = {f for s, f in spy.runs if MARK not in s}
    solver_flags = {f for s, f in spy.runs if MARK in s}
    assert solver_flags
    assert not solver_flags & real  # the attacker's solver only ever holds a throwaway flag
    reqs = [r for r in bob.requests if r.purpose == "attack"]
    assert all(agents.SOLVER_HINT in r.prompt for r in reqs)
    assert all(r.json_schema == agents.ANSWER_SCHEMAS["attack_solver"] for r in reqs)


def test_solver_is_ignored_when_the_rule_is_off(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = ViaSolver()
    state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(rounds=1, challenges_per_round=1))
    (attack,) = _bob_attacks(state)
    assert not attack.solved
    later = [r.prompt for r in bob.requests if r.purpose == "attack"][1:]
    assert later
    assert all("payload='nope'" in p for p in later)
    assert all(agents.SOLVER_HINT not in r.prompt for r in bob.requests)


def test_a_rejected_or_failing_solver_is_a_failed_try(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    for solver, why in (("import socket\nprint(1)", "solver: "), ("print(nothing)", "solver failed (exit 1)")):
        bob = ViaSolver(solver)
        rules = {"rounds": 1, "challenges_per_round": 1, "attacker_solver": True}
        state = make_engine({"alice": FakeProvider(), "bob": bob}).run(new_state(match_id=f"m{len(solver)}", **rules))
        (attack,) = _bob_attacks(state)
        assert not attack.solved
        last = [r.prompt for r in bob.requests if r.purpose == "attack"][-1]
        assert f"payload={MALFORMED!r}" in last
        assert why in last


def test_control_passes_on_a_sound_harness(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    engine = make_engine(metrics=metrics)
    spy = _spy(engine)
    state = engine.run(new_state(rounds=1, challenges_per_round=1, integrity_control=True))
    attempts = sum(a.attempts for a in state.attacks.values())
    assert metrics.counters["control.run"] == attempts == sum(s == CONTROL_SOURCE for s, _ in spy.runs)
    assert not metrics.counters["control.capture"]
    assert not [f for f in state.findings if f.category == "integrity"]


def test_control_capture_flags_a_leaking_harness(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    engine = make_engine(metrics=metrics)
    _spy(engine, leak=True)
    state = engine.run(new_state(rounds=1, challenges_per_round=1, integrity_control=True))
    hits = [f for f in state.findings if f.rule_id == "control-capture"]
    assert hits
    assert {(f.category, f.severity) for f in hits} == {("integrity", "critical")}
    assert metrics.counters["control.capture"] == len(hits)


def test_control_is_off_by_default(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine()
    spy = _spy(engine)
    engine.run(new_state(rounds=1, challenges_per_round=1))
    assert all(s != CONTROL_SOURCE for s, _ in spy.runs)


def test_checked_attack_solver_never_gets_the_real_flag(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice = ViaSolver(f"{MARK}\nprint('28783')")
    engine = make_engine({"alice": alice, "bob": Builds(source=SPARSE, reference_payload="", solver="print('28783')")})
    spy = _spy(engine)
    rules = {"rounds": 1, "challenges_per_round": 1, "challenge_form": "check", "attacker_solver": True}
    state = engine.run(new_state(**rules, solvability=SolvabilityRule(enabled=True)))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    real = MatchEngine._flag_for(state, ch.id)
    solver_flags = {f for s, f in spy.runs if MARK in s}
    assert solver_flags
    assert real not in solver_flags
    assert any(real == f for s, f in spy.runs if MARK not in s)  # the real flag is used, just never for the solver
    assert any(a.solved for a in state.attacks.values() if a.attacker == "alice")
