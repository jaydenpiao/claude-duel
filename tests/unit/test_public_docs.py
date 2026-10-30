"""Public-facing repo docs: owners, reporting channel and generic issue templates."""

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_codeowners_names_maintainer() -> None:
    assert "@jaydenpiao" in (ROOT / ".github" / "CODEOWNERS").read_text()


def test_security_policy_uses_github_reporting() -> None:
    text = (ROOT / "SECURITY.md").read_text()
    assert "Report a vulnerability" in text
    assert "private" not in text.lower()


def test_no_internal_board_or_agent_roles() -> None:
    assert "projects/1" not in (ROOT / "AGENTS.md").read_text()
    for template in (ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.md"):
        text = template.read_text()
        assert "product-scout" not in text
        assert "private" not in text
