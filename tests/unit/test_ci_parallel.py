"""CI runs the suite in parallel with pytest-xdist."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ci_runs_pytest_in_parallel() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert ".venv/bin/pytest -n " in ci


def test_xdist_is_a_dev_dependency() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert any(d.startswith("pytest-xdist") for d in meta["project"]["optional-dependencies"]["dev"])
