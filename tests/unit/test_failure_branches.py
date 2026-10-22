"""Failure branches named in quality review L4: each one gets a targeted test."""

from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path

import pytest

from claude_duel import cli
from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.sandbox import common
from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox
from claude_duel.app import container
from claude_duel.app.engine import GATE
from claude_duel.config import Settings
from claude_duel.domain import policy
from claude_duel.domain.errors import InvalidTransitionError
from claude_duel.domain.models import AgentSpec, Rules, Stage
from claude_duel.domain.state_machine import recover
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest

ONE = {"rounds": 1, "challenges_per_round": 1}
GUARD = "import sys\nif sys.stdin.read().strip() == 'opensesame':\n    print(FLAG)\n"


class Builds:
    """Answers every build with one fixed challenge; attacks go to the fake provider."""

    name = "builds"

    def __init__(self, **spec: object) -> None:
        self.text = json.dumps({"title": "t", "description": "d", "difficulty": 1, **spec})
        self.fake = FakeProvider()

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose != "build":
            return self.fake.complete(request)
        return Completion(text=self.text, input_tokens=10, output_tokens=10)


def _bob(make_engine, new_state, metrics=None, **spec):  # type: ignore[no-untyped-def]
    engine = make_engine({"alice": FakeProvider(), "bob": Builds(**spec)}, metrics=metrics)
    state = engine.run(new_state(**ONE))
    return state, next(c for c in state.challenges.values() if c.author == "bob")


class TestEngine:
    def test_solver_rejected_by_policy(self, make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
        state, mine = _bob(make_engine, new_state, source=GUARD, reference_payload="", solver="import os\n")
        assert state.build_errors[mine.id][0].startswith("solver: ")

    def test_solver_that_fails(self, make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
        solver = "import sys\nsys.exit(3)\n"
        state, mine = _bob(make_engine, new_state, source=GUARD, reference_payload="", solver=solver)
        assert state.build_errors[mine.id][0].startswith("solver failed (exit 3)")

    def test_empty_input_revealing_flag_is_trivial(self, make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
        state, mine = _bob(make_engine, new_state, source="print(FLAG)\n", reference_payload="whatever")
        assert state.validations[mine.id].reason == "trivial: empty input reveals flag"

    def test_killed_run_is_counted(self, make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
        metrics = InMemoryMetrics()
        _bob(make_engine, new_state, metrics, source="while True:\n    pass\n", reference_payload="spin-forever")
        assert metrics.counters["sandbox.killed"] >= 1

    def test_token_budget_ends_the_attack(self, make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
        state = make_engine().run(new_state(**ONE, attack_token_budget=1))
        assert {a.reason for a in state.attacks.values()} <= {"token budget exhausted", "flag captured"}
        assert "token budget exhausted" in {a.reason for a in state.attacks.values()}

    def test_stage_work_is_idempotent(self, make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
        engine = make_engine()
        state = engine.run(new_state(**ONE))
        before = len(event_log.read(state.match_id))
        engine._build(state)
        engine._validate(state)
        engine._score(state)
        assert len(event_log.read(state.match_id)) == before


def test_recover_refuses_a_live_match(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state()
    assert state.stage is not Stage.FAILED
    with pytest.raises(InvalidTransitionError):
        recover(state)


class TestPolicy:
    def test_int_sequence_of_another_length_is_not_the_answer(self) -> None:
        assert policy.answer_exposed(ast.parse("x = [1, 2, 3, 4, 5]"), "abcd") is None

    def test_format_field_attribute_access(self) -> None:
        assert policy.check_source("print('{0.x}'.format(1))\n", 4096) == "format field with attribute or index access"

    def test_star_import(self) -> None:
        assert policy.check_source("from math import *\n", 4096) == "star import not allowed"

    def test_restricted_module_attribute(self) -> None:
        assert (policy.check_source("import sys\nsys.argv\n", 4096) or "").startswith("banned attribute: sys.argv")


def test_write_to_a_closed_pipe_is_ignored() -> None:
    class Broken(io.BytesIO):
        def write(self, data: object) -> int:
            raise BrokenPipeError

    common._write(Broken(), b"x")


class TestContainer:
    def test_healthy_docker_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(DockerSandbox, "health", lambda self: None)
        assert isinstance(container.make_sandbox(Settings(), Settings().agents), DockerSandbox)

    def test_gate_is_wired_when_enabled(self, tmp_path: Path) -> None:
        settings = Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(difficulty_gate=True))
        agents = (AgentSpec(name="a"), AgentSpec(name="b"))
        engine = container.build_engine(settings, InMemoryMetrics(), container.build_store(settings), agents)
        assert GATE in engine._providers


class TestCli:
    def test_broken_pipe_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        silenced: list[bool] = []

        def boom(args: object) -> int:
            raise BrokenPipeError

        monkeypatch.setattr(cli, "_dispatch", boom)
        monkeypatch.setattr(cli, "_silence_stdout", lambda: silenced.append(True))
        assert cli.main(["replay", "x"]) == 0
        assert silenced == [True]

    def test_silence_points_stdout_at_devnull(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with (tmp_path / "out").open("w") as fake:
            monkeypatch.setattr(cli.sys, "stdout", fake)
            cli._silence_stdout()
            os.write(fake.fileno(), b"gone")
        assert (tmp_path / "out").read_bytes() == b""
