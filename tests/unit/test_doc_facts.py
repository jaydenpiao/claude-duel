"""Docs that must agree with the code and with each other (changelog shape, ADR checklists, field wording)."""

import re
from pathlib import Path

from claude_duel.app.budget import BudgetLimits

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _unreleased() -> str:
    return CHANGELOG.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]


def test_unreleased_has_each_section_once() -> None:
    heads = re.findall(r"^### (\w+)", _unreleased(), re.MULTILINE)
    assert heads
    assert len(heads) == len(set(heads)), heads


def test_accepted_adr_open_boxes_wait_on_unmerged_work() -> None:
    merged = set(re.findall(r"#(\d+)", _unreleased()))
    for p in (ROOT / "docs" / "adr").glob("*.md"):
        text = p.read_text(encoding="utf-8")
        if "accepted" not in text.split("\n", 3)[2].lower():
            continue
        for line in (x for x in text.splitlines() if x.startswith("- [ ]")):
            m = re.search(r"waits on #(\d+)", line)
            assert m, (p.name, line)
            assert m.group(1) not in merged, (p.name, f"#{m.group(1)} merged; tick the box")


def test_key_limit_description_is_a_ceiling() -> None:
    desc = BudgetLimits.model_fields["key_limit_usd"].description or ""
    assert "lifetime_max_usd" in desc
    assert "shown in output" not in desc
