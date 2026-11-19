"""No-fluke ladder: ``round_weight="flat"`` scores every round at weight 1; linear (default) is unchanged."""

from claude_duel.domain.models import ChallengeSpec, Rules, ScoringRule
from claude_duel.domain.scoring import defense_award, solve_award, weight

SPEC = ChallengeSpec(title="t", description="d", source="print(1)", reference_payload="x" * 12, difficulty=3)
FLAT = ScoringRule(round_weight="flat")


def test_linear_is_the_default_and_unchanged() -> None:
    rule = ScoringRule()
    assert rule.round_weight == "linear"
    assert [weight(r, rule) for r in (1, 2, 3)] == [1, 2, 3]
    assert defense_award(SPEC, 3, rule) == 90
    assert solve_award(SPEC, 3, 2, rule) == 89


def test_flat_weights_every_round_the_same() -> None:
    assert {defense_award(SPEC, r, FLAT) for r in range(1, 13)} == {30}
    assert {solve_award(SPEC, r, 1, FLAT) for r in range(1, 13)} == {30}
    assert FLAT.defense_points >= FLAT.solve_points  # ADR 0006 holds per round


def test_old_rules_without_the_field_load_linear() -> None:
    data = Rules().dump()
    data["scoring"].pop("round_weight")
    assert Rules.model_validate(data).scoring.round_weight == "linear"
