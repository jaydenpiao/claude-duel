"""Every design spec named in code, tests or docs ships in docs/design."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"\bdesign-[a-z0-9-]+\.md\b")


def test_design_spec_mentions_resolve() -> None:
    shipped = {p.name for p in (ROOT / "docs" / "design").glob("design-*.md")}
    named = {
        (m, str(p.relative_to(ROOT)))
        for base in ("src", "tests", "docs/adr")
        for p in (ROOT / base).rglob("*")
        if p.suffix in {".py", ".md"}
        for m in NAME.findall(p.read_text(encoding="utf-8"))
    }
    assert {m for m, _ in named} >= {"design-core-logic.md", "design-reasoning-models.md"}
    assert [(m, where) for m, where in sorted(named) if m not in shipped] == []
