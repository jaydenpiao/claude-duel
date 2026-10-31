"""Accepted ADRs tick what is built; an open box must say what it waits on."""

import re
from pathlib import Path

ADR = Path(__file__).resolve().parents[2] / "docs" / "adr"


def test_accepted_adr_open_boxes_name_their_blocker() -> None:
    for p in ADR.glob("*.md"):
        text = p.read_text(encoding="utf-8")
        if "accepted" not in text.split("\n", 3)[2].lower():
            continue
        open_boxes = [line for line in text.splitlines() if line.startswith("- [ ]")]
        assert all(re.search(r"waits on #\d+", line) for line in open_boxes), (p.name, open_boxes)
