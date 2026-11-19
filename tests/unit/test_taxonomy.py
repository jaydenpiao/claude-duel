"""Design rec R2: a seeded family taxonomy shared by both agents, and prompts without fixed anchors."""

from __future__ import annotations

import pytest

from claude_duel.app import agents
from claude_duel.domain import variety
from claude_duel.domain.modes import preset

AGENTS = ["haiku", "nano"]


@pytest.mark.parametrize(("rounds", "per_round"), [(3, 2), (6, 2), (4, 3)])
def test_draw_is_pure_shared_and_never_repeats(rounds: int, per_round: int) -> None:
    for n in range(1000):
        seed = f"s{n}"
        seen: dict[str, set[str]] = {a: set() for a in AGENTS}
        locks = 0
        for r in range(1, rounds + 1):
            for i in range(per_round):
                (ax1, f1), (ax2, f2) = (variety.family(seed, AGENTS, a, r, i, per_round) for a in AGENTS)
                assert (ax1, f1) == variety.family(seed, AGENTS, AGENTS[0], r, i, per_round)
                assert ax1 == ax2
                assert f1 != f2
                assert f1 in variety.TAXONOMY[ax1]
                locks += ax1 == variety.LOCK_AXIS
                for a, f in zip(AGENTS, (f1, f2), strict=True):
                    assert f not in seen[a]
                    seen[a].add(f)
        assert locks <= 1


def test_different_seeds_give_different_orders() -> None:
    draws = {tuple(variety.slot_axis(f"s{n}", k) for k in range(6)) for n in range(50)}
    assert len(draws) > 10


def test_check_prompt_has_no_fixed_family_anchors() -> None:
    text = "\n".join(agents._check_rules(preset("checked"), 1))
    assert "range(100000)" not in text.split("One valid example")[0]
    assert "a tiny VM, a grid walk" not in text
    assert text.count('{"title": ') == 0  # the one example comes from variety.prompt_lines (axis pool)
    assert "constructively" in text
    program = "\n".join(agents.build_rules(preset("classic"), 1))
    assert "tiny stack VM" not in program
