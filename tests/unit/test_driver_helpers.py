"""Shared driver helpers: open_or_create, append, pinned wording, spend caps."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.app.budget import SpendCap
from claude_duel.app.driver import append, check_caps, open_or_create, pinned
from claude_duel.config import Settings
from claude_duel.domain.errors import BudgetExceededError, EventLogCorruptError
from claude_duel.domain.models import Rules, SandboxLimits
from claude_duel.domain.series import SERIES_CREATED, SERIES_SCHEMA_VERSION, fold


def test_open_or_create_writes_once(tmp_path: Path) -> None:
    log = FileEventLog(tmp_path)
    agents = [{"name": "a", "provider": "fake"}, {"name": "b", "provider": "fake"}]
    data = {
        "seed": "abc",
        "rule": {"games": 1},
        "agents": agents,
        "rules": Rules().model_dump(mode="json"),
        "limits": SandboxLimits().model_dump(mode="json"),
    }
    first = open_or_create(log, "s1", schema_version=SERIES_SCHEMA_VERSION, created_type=SERIES_CREATED, data=data)
    again = open_or_create(
        log, "s1", schema_version=SERIES_SCHEMA_VERSION, created_type=SERIES_CREATED, data={"seed": "nope"}
    )
    assert len(first) == 1 == len(again)
    assert first[0].type == SERIES_CREATED
    assert fold("s1", again).series_id == "s1"


def test_append_sets_seq(tmp_path: Path) -> None:
    log = FileEventLog(tmp_path)
    agents = [{"name": "a", "provider": "fake"}, {"name": "b", "provider": "fake"}]
    open_or_create(
        log,
        "s1",
        schema_version=SERIES_SCHEMA_VERSION,
        created_type=SERIES_CREATED,
        data={
            "seed": "s",
            "rule": {"games": 1},
            "agents": agents,
            "rules": Rules().model_dump(mode="json"),
            "limits": SandboxLimits().model_dump(mode="json"),
        },
    )
    ev = append(
        log,
        "s1",
        schema_version=SERIES_SCHEMA_VERSION,
        seq=1,
        event_type="game_finished",
        data={"game_no": 1, "match_id": "s1-g1", "winner": "a", "scores": {}, "usd": 0.0},
    )
    assert ev.seq == 1
    assert log.read("s1")[-1].type == "game_finished"


def test_pinned_warns_with_neutral_wording(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(rules=Rules(rounds=1), limits=SandboxLimits())
    other = Rules(rounds=2)
    with caplog.at_level(logging.WARNING):
        out = pinned(settings, other, SandboxLimits(), "tid")
    assert "saved driver log" in caplog.text
    assert out.rules == other


def test_check_caps_raises(tmp_path: Path) -> None:
    settings = Settings(store_dir=tmp_path)
    # empty ledger: spent 0; cap at 0 raises
    with pytest.raises(BudgetExceededError, match="tournament"):
        check_caps(settings, (SpendCap("tournament", "t-", 0.0),))


def test_fold_created_rejects_empty() -> None:
    with pytest.raises(EventLogCorruptError, match="series_created"):
        fold("s1", [])
