"""Sealed Ledger step 3b: seeded terrain (design-core-logic.md acceptance tests 2 and 3)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.domain import terrain

TABLE = [
    "identity",
    "reverse",
    "rot13",
    "swapcase",
    "b64",
    *(f"xor:{b:02x}" for b in (1, 0x1F)),
    "prefix:1",
    "prefix:4",
]


@pytest.mark.parametrize("t", TABLE)
@given(x=st.text(max_size=200))
def test_every_transform_round_trips(t: str, x: str) -> None:
    assert terrain.invert(t, terrain.apply(t, x)) == x


@pytest.mark.parametrize("t", TABLE)
@given(y=st.text(max_size=200))
def test_pre_inverted_payload_reaches_the_program_unchanged(t: str, y: str) -> None:
    x = terrain.invert(t, y)
    if x is not None:
        assert terrain.apply(t, x) == y


def test_draw_is_reproducible_and_seed_dependent() -> None:
    ids = [f"alice:{i}" for i in range(12)]
    a = [terrain.draw("s1", 1, c) for c in ids]
    assert a == [terrain.draw("s1", 1, c) for c in ids]
    assert a != [terrain.draw("s2", 1, c) for c in ids]
    assert a != [terrain.draw("s1", 2, c) for c in ids]
    assert {t.partition(":")[0] for t in a} <= set(terrain.KINDS)


def test_ref_without_inverse_falls_back_to_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terrain, "draw", lambda *_: "b64")
    assert terrain.draw_round("s", 1, [("a", "x:0", "not base64!"), ("b", "x:1", "aGk=")]) == {
        "a": "identity",
        "b": "b64",
    }


def test_describe_covers_every_kind() -> None:
    for t in TABLE:
        assert terrain.describe(t)
