"""Deterministic, pure scoring."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from claude_duel.domain.models import Attack, Challenge, ChallengeSpec, MatchState, Rules, ScoringRule, Validation


@dataclass(frozen=True)
class RoundFacts:
    """Everything a scorer may look at for one round; built only from event-sourced state."""

    round_no: int
    agents: tuple[str, ...]
    challenges: tuple[Challenge, ...]  # this round's, sorted by id
    validations: Mapping[str, Validation]
    attacks: Mapping[str, Attack]  # by challenge id


Scorer = Callable[[RoundFacts, ScoringRule], dict[str, int]]


def score_round(
    round_no: int,
    agents: Iterable[str],
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    attacks: Mapping[str, Attack],
    rules: Rules,
) -> dict[str, int]:
    facts = RoundFacts(
        round_no=round_no,
        agents=tuple(agents),
        challenges=tuple(sorted((c for c in challenges if c.round == round_no), key=lambda c: c.id)),
        validations=validations,
        attacks={a.challenge_id: a for a in attacks.values()},
    )
    return SCORERS[rules.scoring.kind](facts, rules.scoring)


def classic(facts: RoundFacts, rule: ScoringRule) -> dict[str, int]:
    """ADR 0006 scoring: invalid costs the author, a capture pays the attacker, a defense pays the author."""
    scores = dict.fromkeys(facts.agents, 0)
    for ch in facts.challenges:
        v = facts.validations.get(ch.id)
        if ch.spec is None or v is None or not v.valid:
            scores[ch.author] -= rule.invalid_penalty * facts.round_no
            continue
        atk = facts.attacks.get(ch.id)
        if atk is not None and atk.solved:
            scores[atk.attacker] += solve_award(ch.spec, facts.round_no, atk.attempts, rule)
        else:
            scores[ch.author] += defense_award(ch.spec, facts.round_no, rule)
    return scores


def efficiency(facts: RoundFacts, rule: ScoringRule) -> dict[str, int]:
    """Classic, except a capture is discounted by the attack's recorded tokens (never below 1; defenses unchanged)."""
    scores = classic(facts, rule)
    for ch in facts.challenges:
        atk = facts.attacks.get(ch.id)
        v = facts.validations.get(ch.id)
        if ch.spec is not None and v is not None and v.valid and atk is not None and atk.solved:
            full = solve_award(ch.spec, facts.round_no, atk.attempts, rule)
            scores[atk.attacker] -= full - efficiency_award(full, atk.tokens_used, rule)
    return scores


def efficiency_award(full: int, tokens_used: int, rule: ScoringRule) -> int:
    """A capture's value after the token discount: monotone non-increasing in tokens, always >= 1."""
    return max(1, full - rule.max_penalty * tokens_used // rule.reference_tokens)


SCORERS: dict[str, Scorer] = {"classic": classic, "efficiency": efficiency}


def solve_award(spec: ChallengeSpec, round_no: int, attempts: int, rule: ScoringRule) -> int:
    """Points for capturing a flag: weighted by difficulty and round, minus one per extra try."""
    return max(1, rule.solve_points * spec.difficulty * round_no - (attempts - 1))


def defense_award(spec: ChallengeSpec, round_no: int, rule: ScoringRule) -> int:
    """Points for an author whose valid challenge survived the attack."""
    return rule.defense_points * spec.difficulty * round_no


def add_scores(total: Mapping[str, int], delta: Mapping[str, int]) -> dict[str, int]:
    keys = sorted(set(total) | set(delta))
    return {k: total.get(k, 0) + delta.get(k, 0) for k in keys}


def match_stats(state: MatchState) -> dict[str, float]:
    """Outcome rates for run reports: how many challenges validated, and how attacks on them ended."""
    built = len(state.challenges)
    valid = sum(v.valid for v in state.validations.values())
    attacks = list(state.attacks.values())
    captures = sum(a.solved for a in attacks)
    return {
        "challenges": built,
        "valid": valid,
        "valid_rate": round(valid / built, 3) if built else 0.0,
        "captures": captures,
        "defenses": len(attacks) - captures,
        "capture_rate": round(captures / len(attacks), 3) if attacks else 0.0,
    }
