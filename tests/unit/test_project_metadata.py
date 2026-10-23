"""Release gate: publishable pyproject metadata and a CHANGELOG entry for the current version."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_metadata_fields() -> None:
    assert PROJECT["authors"]
    assert {"ctf", "llm"} <= set(PROJECT["keywords"])
    assert {"Homepage", "Source", "Issues", "Changelog"} <= set(PROJECT["urls"])


def test_classifiers_match_requires_python() -> None:
    versions = {
        c.rsplit(" ", 1)[1] for c in PROJECT["classifiers"] if c.startswith("Programming Language :: Python :: 3.")
    }
    assert versions == {"3.11", "3.12", "3.13"}
    assert PROJECT["requires-python"] == ">=3.11"
    assert "Operating System :: POSIX" in PROJECT["classifiers"]


def test_changelog_has_current_version() -> None:
    assert f"## [{PROJECT['version']}]" in (ROOT / "CHANGELOG.md").read_text()
