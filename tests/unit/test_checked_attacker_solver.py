"""Design rec R3: in the checked preset attackers may send a solver, so the author's budget is the attacker's."""

from claude_duel.domain.modes import preset


def test_checked_preset_gives_attackers_a_solver() -> None:
    rules = preset("checked")
    assert rules.attacker_solver
    assert rules.solvability.solver_cap(rules.attacker_solver) == 200_000
    assert not preset("classic").attacker_solver
