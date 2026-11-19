"""An attack-time too-slow void is neutral (0 for both sides); a validation-time too-slow keeps the penalty."""

from claude_duel.domain.models import ATTACK_TIMEOUT, Challenge, ChallengeSpec, ScoringRule, Validation
from claude_duel.domain.scoring import RoundFacts, classic

SPEC = ChallengeSpec(title="t", description="d", source="print(1)", reference_payload="x" * 12, difficulty=2)
CH = Challenge(id="c", round=2, author="bob", spec=SPEC)
RULE = ScoringRule()


def _scores(reason: str) -> dict[str, int]:
    facts = RoundFacts(
        round_no=2,
        agents=("alice", "bob"),
        challenges=(CH,),
        validations={"c": Validation(challenge_id="c", valid=False, reason=reason)},
        attacks={},
    )
    return classic(facts, RULE)


def test_attack_time_void_scores_zero_for_both() -> None:
    assert _scores(ATTACK_TIMEOUT) == {"alice": 0, "bob": 0}


def test_validation_time_too_slow_keeps_the_penalty() -> None:
    reason = "too slow: junk input 'a' timed out or was killed; check must answer every input quickly"
    assert _scores(reason) == {"alice": 0, "bob": -RULE.invalid_penalty * 2}
