"""Observers never affect the match (architecture round 2, N4)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.observers import IsolatedObserver
from claude_duel.app.replay import replay
from claude_duel.domain.models import Event, Stage
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest


class Exploding:
    def __init__(self, exc: Exception, after: int = 0) -> None:
        self.exc, self.after, self.seen = exc, after, 0

    def __call__(self, event: Event) -> None:
        self.seen += 1
        if self.seen > self.after:
            raise self.exc


class Crashing:
    name = "crashing"

    def __init__(self) -> None:
        self.inner, self.calls = FakeProvider(), 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        if self.calls >= 3:
            raise RuntimeError("provider down")
        return self.inner.complete(request)


@pytest.mark.parametrize("exc", [BrokenPipeError(), KeyError("x"), AssertionError("bug")])
def test_failing_observer_does_not_affect_match(make_engine, new_state, event_log, exc) -> None:  # type: ignore[no-untyped-def]
    metrics = InMemoryMetrics()
    observer = Exploding(exc, after=2)
    state = make_engine(metrics=metrics, observer=observer).run(new_state())
    assert state.stage is Stage.COMPLETE
    assert replay(state, event_log.read("m1")).consistent
    assert observer.seen == 3  # disabled after the first failure
    assert metrics.counters["observer.error"] == 1


def test_original_error_is_not_masked(make_engine, new_state, store) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine({"alice": FakeProvider(), "bob": Crashing()}, observer=Exploding(BrokenPipeError()))
    with pytest.raises(RuntimeError, match="provider down"):
        engine.run(new_state())
    saved = store.load("m1")
    assert saved.stage is Stage.FAILED
    assert "provider down" in (saved.failure or "")


def test_isolated_observer_passes_events_through() -> None:
    seen: list[int] = []
    IsolatedObserver(lambda e: seen.append(e.seq), InMemoryMetrics())(Event(seq=0, type="x", trace_id="t", data={}))
    assert seen == [0]


def test_cli_survives_closed_pipe(tmp_path: Path) -> None:
    env = {**os.environ, "DUEL_STORE_DIR": str(tmp_path), "DUEL_SANDBOX": "subprocess", "DUEL_ROUNDS": "1"}
    env.pop("OPENAI_API_KEY", None)
    cli = subprocess.Popen(
        [sys.executable, "-m", "claude_duel.cli", "run", "--match-id", "pipe"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert cli.stdout is not None
    cli.stdout.readline()
    cli.stdout.close()  # like `| head -1`
    _, err = cli.communicate(timeout=60)
    assert cli.returncode == 0
    assert b"Traceback" not in err
    assert '"stage": "complete"' in (tmp_path / "pipe.json").read_text()
