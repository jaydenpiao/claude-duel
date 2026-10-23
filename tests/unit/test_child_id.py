"""Tournament child id normalisation (L2)."""

from __future__ import annotations

from claude_duel.app.tournament import _child_id


def test_child_id_joins_and_normalises_underscores() -> None:
    assert _child_id("t1", "pool", "x_y") == "t1-pool-x-y"
    assert _child_id("tid", "r1", "a_b", "c") == "tid-r1-a-b-c"
