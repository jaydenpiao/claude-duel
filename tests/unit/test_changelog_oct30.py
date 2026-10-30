"""CHANGELOG covers the Oct 29-30 merges (quality review L7)."""

from pathlib import Path

TEXT = (Path(__file__).parents[2] / "CHANGELOG.md").read_text()


def test_changelog_lists_oct30_merges() -> None:
    for entry in ("lifetime cap", "Shared driver helpers", "Review-code gate", "Integrity control"):
        assert entry in TEXT
