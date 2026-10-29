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
            award = defense_award(ch.spec, facts.round_no, rule)
            scores[ch.author] += award // 2 if ch.probe_solved else award
    return scores


def _discounted(
    facts: RoundFacts,
    rule: ScoringRule,
    award: Callable[[int, int, ScoringRule], int],
    cost: Callable[[Attack, ScoringRule], int] = lambda atk, _rule: atk.tokens_used,
) -> dict[str, int]:
    """Classic, with each capture's value replaced by ``award(full, cost(attack), rule)``; defenses unchanged."""
    scores = classic(facts, rule)
    for ch in facts.challenges:
        atk = facts.attacks.get(ch.id)
        v = facts.validations.get(ch.id)
        if ch.spec is not None and v is not None and v.valid and atk is not None and atk.solved:
            full = solve_award(ch.spec, facts.round_no, atk.attempts, rule)
            scores[atk.attacker] -= full - award(full, cost(atk, rule), rule)
    return scores


def efficiency(facts: RoundFacts, rule: ScoringRule) -> dict[str, int]:
    """Classic, except a capture is discounted by the attack's recorded tokens (never below 1; defenses unchanged)."""
    return _discounted(facts, rule, efficiency_award, cost_used)


def cost_used(atk: Attack, rule: ScoringRule) -> int:
    """The attack's cost in efficiency units, as reference_tokens-scaled tokens (``unit="usd"`` rescales micro-$)."""
    if rule.unit == "usd":
        return atk.usd_micros * rule.reference_tokens // rule.reference_usd_micros
    return atk.tokens_used


def timed(facts: RoundFacts, rule: ScoringRule) -> dict[str, int]:
    """Blitz scoring (#36): a slow capture (in recorded tokens against ``par_tokens``) is worth less, never below 1."""
    return _discounted(facts, rule, timed_award)


def timed_award(full: int, tokens_used: int, rule: ScoringRule) -> int:
    slow = min(full - 1, rule.max_penalty * min(tokens_used, rule.par_tokens) // rule.par_tokens)
    return full - slow


def efficiency_award(full: int, tokens_used: int, rule: ScoringRule) -> int:
    """A capture's value after the token discount: monotone non-increasing in tokens, always >= 1."""
    return max(1, full - rule.max_penalty * tokens_used // rule.reference_tokens)


def ledger(facts: RoundFacts, rule: ScoringRule) -> dict[str, int]:
    """Sealed Ledger (design-core-logic.md 4.3): classic, but a capture is discounted by the share of its stake used.

    A stake of 0 means the attacker skipped the challenge, so it scores as a defense. Discount is 0..50%, never
    below 1, so defense >= capture (ADR 0006) still holds.
    """
    skipped = {cid: atk.model_copy(update={"solved": False}) for cid, atk in facts.attacks.items() if atk.stake == 0}
    if skipped:
        facts = RoundFacts(
            facts.round_no, facts.agents, facts.challenges, facts.validations, {**facts.attacks, **skipped}
        )
    scores = classic(facts, rule)
    for ch in facts.challenges:
        atk = facts.attacks.get(ch.id)
        v = facts.validations.get(ch.id)
        if ch.spec is not None and v is not None and v.valid and atk is not None and atk.solved and atk.stake:
            full = solve_award(ch.spec, facts.round_no, atk.attempts, rule)
            scores[atk.attacker] -= full - stake_award(full, atk.tokens_used, atk.stake)
    return scores


def stake_award(full: int, used: int, stake: int) -> int:
    """A capture's value: ``full`` less up to half, by the share of ``stake`` used; always in [1, full]."""
    return max(1, full - full * min(used, stake) // (2 * stake))


SCORERS: dict[str, Scorer] = {"classic": classic, "efficiency": efficiency, "timed": timed, "ledger": ledger}


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
