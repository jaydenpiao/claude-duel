"""The release ADR records that the public repo runs on GitHub-hosted runners only (#163)."""

from pathlib import Path

ADR = Path(__file__).parents[2] / "docs" / "adr" / "0005-release-and-environments.md"


def test_adr_pins_public_repo_to_hosted_runners() -> None:
    text = ADR.read_text()
    assert "#163" in text
    assert "GitHub-hosted runners" in text
    assert "not a security control" in text
