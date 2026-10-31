"""Prophecy menu tier (design-deep-layer.md acceptance 10, 12-14): pure, sealed, bounded."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.domain.prophecy import (
    TEMPLATES,
    Challenge,
    Prophecy,
    RoundArtifacts,
    commit,
    holds,
    pick,
    settle,
    verify,
)

A = Challenge(cid="c1", size=100, tag="hash-check")
B = Challenge(cid="c2", size=400, tag="vm")
HIT = RoundArtifacts(challenges=(A, B), keystone="c2", captures=0, ghost_kills=0, stakes=(6_000, 0))
MISS = RoundArtifacts(challenges=(B,), keystone=None, captures=3, ghost_kills=2, stakes=(1_000,))
C = Challenge(cid="c3", size=50, tag="checksum")
SHORT = RoundArtifacts(challenges=(A, B), keystone="c1", captures=2, ghost_kills=1, stakes=(1_000,))
CHK = RoundArtifacts(challenges=(C,), keystone="c3")
MAZE = RoundArtifacts(
    challenges=(Challenge(cid="c4", size=200, tag="maze"), Challenge(cid="c5", size=300, tag="state-machine"))
)
FIXTURES = {  # template -> (settles true, settles false)
    "keystone_longest": (HIT, SHORT),
    "keystone_shortest": (SHORT, HIT),
    "keystone_first": (SHORT, HIT),
    "uses_maze": (MAZE, HIT),
    "uses_vm": (HIT, CHK),
    "uses_checksum": (CHK, HIT),
    "uses_state_machine": (MAZE, HIT),
    "ghost_kill": (MISS, HIT),
    "no_captures": (HIT, MISS),
    "one_capture_max": (HIT, MISS),
    "all_in": (HIT, MISS),
    "longest_keystone_no_ghost": (HIT, SHORT),
}


def p(name: str, stake: int = 1) -> Prophecy:
    return Prophecy(template=name, atoms=TEMPLATES[name], stake=stake)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_settles_both_ways(name: str) -> None:
    true_art, false_art = FIXTURES[name]
    assert holds(p(name), true_art)
    assert not holds(p(name), false_art)


def test_twelve_templates() -> None:
    assert len(TEMPLATES) == len(FIXTURES) == 12


def test_commit_matches_reveal_and_a_tampered_reveal_fails() -> None:
    sealed = commit(p("uses_vm", 2), "salt")
    assert verify(p("uses_vm", 2), "salt", sealed)
    assert not verify(p("uses_vm", 3), "salt", sealed)
    assert not verify(p("uses_maze", 2), "salt", sealed)


def test_invalid_json_is_skipped_not_replaced() -> None:
    for raw in (
        "nope",
        "{}",
        '{"prophecies": []}',
        '{"prophecies": [{"template": "x", "stake": 1}]}',
        '{"prophecies": [{"template": "uses_vm", "stake": 9}]}',
    ):
        assert pick(raw) is None
    got = pick(json.dumps({"prophecies": [{"template": "uses_vm", "stake": 2}]}))
    assert got == [p("uses_vm", 2)]


def test_vacuous_templates_pay_zero() -> None:
    kw = {"points": 5, "swing": 0, "pool": 6, "round_cap": 3}
    assert settle([p("no_captures", 3)], HIT, rates={"no_captures": 0.97}, **kw)[0] == 0
    assert settle([p("no_captures", 3)], HIT, rates={"no_captures": 0.5}, **kw)[0] == 3


@given(
    st.lists(st.tuples(st.sampled_from(sorted(TEMPLATES)), st.integers(1, 3)), min_size=1, max_size=2).map(
        lambda xs: [p(n, s) for n, s in xs]
    ),
    st.sampled_from([HIT, MISS]),
    st.integers(0, 6),
    st.integers(0, 6),
    st.integers(1, 6),
)
def test_points_never_negative_and_swing_bounded(
    ps: list[Prophecy], art: RoundArtifacts, points: int, swing: int, cap: int
) -> None:
    delta, hits = settle(ps, art, points=points, swing=swing, pool=6, round_cap=cap, rates={})
    assert len(hits) == len(ps)
    assert points + delta >= 0
    assert abs(delta) <= cap
    assert swing + abs(delta) <= max(6, swing)
