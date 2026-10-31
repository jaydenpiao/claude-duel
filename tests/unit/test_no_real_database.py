"""Tests never see a developer's real results-db URLs."""

from __future__ import annotations

import os

from tests.conftest import _REAL_DB_VARS


def test_real_database_urls_are_cleared() -> None:
    assert not any(v in os.environ for v in _REAL_DB_VARS)
