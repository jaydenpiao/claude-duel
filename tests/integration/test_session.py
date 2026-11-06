"""Session: lock before load, stale locks, resume keeps the saved config."""

from __future__ import annotations

import logging
import multiprocessing
import os
from pathlib import Path

import pytest

from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain.errors import MatchLockedError
from claude_duel.domain.models import AgentSpec, Rules, Stage
from claude_duel.observability import InMemoryMetrics


def _settings(root: Path, **kw: object) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1), **kw)  # type: ignore[arg-type]


def _hold_lock_and_die(root: str) -> None:
    FileMatchStore(Path(root)).lock("m1").__enter__()
    os._exit(0)  # simulate SIGKILL: no __exit__, no cleanup


def test_lock_is_held_before_load(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with FileMatchStore(tmp_path).lock("m1"), pytest.raises(MatchLockedError):
        play(settings, "m1", InMemoryMetrics())
    assert not (tmp_path / "m1.json").exists()  # nothing was loaded or created under someone else's lock


def test_stale_lock_from_dead_process_is_reclaimed(tmp_path: Path) -> None:
    proc = multiprocessing.get_context("spawn").Process(target=_hold_lock_and_die, args=(str(tmp_path),))
    proc.start()
    proc.join()
    assert (tmp_path / "m1.lock").exists()
    assert play(_settings(tmp_path), "m1", InMemoryMetrics()).stage is Stage.COMPLETE


def test_lock_is_not_reentrant(tmp_path: Path) -> None:
    lock = FileMatchStore(tmp_path).lock("m1")
    with lock, pytest.raises(MatchLockedError), lock:
        pass


def test_resume_uses_saved_config(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    first = _settings(tmp_path, agents=(AgentSpec(name="alice", skill=2), AgentSpec(name="bob", skill=5)))
    done = play(first, "m1", InMemoryMetrics())
    assert [a.model for a in done.agents] == ["fake-1", "fake-1"]  # effective model pinned at creation
    changed = Settings(
        store_dir=tmp_path,
        sandbox="subprocess",
        rules=Rules(rounds=3),
        agents=(AgentSpec(name="carol"), AgentSpec(name="dave")),
    )
    with caplog.at_level(logging.WARNING):
        again = play(changed, "m1", InMemoryMetrics())
    assert again == done  # complete match untouched; carol/dave never wired
    assert "resuming with the saved config" in caplog.text


def test_resume_with_unchanged_config_does_not_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(tmp_path)
    play(settings, "m1", InMemoryMetrics())
    with caplog.at_level(logging.WARNING):
        play(settings, "m1", InMemoryMetrics())
    assert "saved config" not in caplog.text
