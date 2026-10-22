"""Release gate H2: a Contributor Covenant 2.1 code of conduct with a contact, linked from CONTRIBUTING."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_code_of_conduct_is_covenant_2_1_with_contact() -> None:
    text = (ROOT / "CODE_OF_CONDUCT.md").read_text()
    assert "Contributor Covenant" in text
    assert "version 2.1" in text
    assert "[INSERT" not in text


def test_contributing_links_code_of_conduct() -> None:
    assert "(CODE_OF_CONDUCT.md)" in (ROOT / "CONTRIBUTING.md").read_text()
