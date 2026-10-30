"""Deterministic, pure scoring."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.ghosts import ghost_award
from claude_duel.domain.heat import heat_lines
from claude_duel.domain.models import Attack, Challenge, ChallengeSpec, MatchState, Rules, ScoringRule, Validation


@dataclass(frozen=True)
class RoundFacts:
    """Everything a scorer may look at for one round; built only from event-sourced state."""

    round_no: int
    agents: tuple[str, ...]
    challenges: tuple[Challenge, ...]  # this round's, sorted by id
    validations: Mapping[str, Validation]
    attacks: Mapping[str, Attack]  # by challenge id
    keystones: Mapping[str, str] = field(default_factory=dict)  # author -> revealed keystone challenge id (ledger)
    heat: Mapping[str, int] = field(default_factory=dict)  # challenge id -> heat_penalty line (ledger, ADR 0006 add.)
    ghost_hits: Mapping[str, str] = field(default_factory=dict)  # challenge id -> ghost from the log (deep ghosts on)


Scorer = Callable[[RoundFacts, ScoringRule], dict[str, int]]


def score_round(
    round_no: int,
    agents: Iterable[str],
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    attacks: Mapping[str, Attack],
    rules: Rules,
    keystones: Mapping[str, str] | None = None,
    ghost_hits: Mapping[str, str] | None = None,
) -> dict[str, int]:
    challenges = list(challenges)
    by_challenge = {a.challenge_id: a for a in attacks.values()}
    facts = RoundFacts(
        round_no=round_no,
        agents=tuple(agents),
        challenges=tuple(sorted((c for c in challenges if c.round == round_no), key=lambda c: c.id)),
        validations=validations,
        attacks={a.challenge_id: a for a in attacks.values()},
        keystones=keystones or {},
        heat=round_heat(challenges, validations, by_challenge, round_no, rules),
        ghost_hits=ghost_hits or {},
    )
    return SCORERS[rules.scoring.kind](facts, rules.scoring)


def capture_points(
    round_no: int,
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    attacks: Mapping[str, Attack],
    rules: Rules,
) -> dict[str, int]:
    """Ledger: what each staked capture of the round paid its attacker, by challenge id (logged, never folded)."""
    if rules.ledger is None:
        return {}
    by_cid = {a.challenge_id: a for a in attacks.values()}
    mine = [c for c in challenges if c.round == round_no]
    return {
        cid: paid for cid, (_, _, paid) in sorted(_captures(mine, validations, by_cid, round_no, rules.scoring).items())
    }


def _captures(
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    attacks: Mapping[str, Attack],
    round_no: int,
    rule: ScoringRule,
) -> dict[str, tuple[str, int, int]]:
    """Ledger captures: challenge id -> (attacker, what classic paid, what the capture pays). Staked captures are
    discounted by stake use; ghost captures (stake 0, deep ghosts) pay ``ghost_award``. The one source for both the
    ledger scorer and the logged ``points``."""
    out = {}
    for ch in challenges:
        atk = attacks.get(ch.id)
        v = validations.get(ch.id)
        if ch.spec is None or v is None or not v.valid or atk is None or not atk.solved:
            continue
        classic_paid = solve_award(ch.spec, round_no, max(1, atk.attempts), rule)  # ghosts log attempts=0
        if atk.ghost is not None:
            out[ch.id] = (atk.attacker, classic_paid, ghost_award(classic_paid))
        elif atk.stake:
            out[ch.id] = (atk.attacker, classic_paid, stake_award(classic_paid, atk.tokens_used, atk.stake))
    return out


def round_heat(
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    attacks: Mapping[str, Attack],
    round_no: int,
    rules: Rules,
) -> dict[str, int]:
    """The round's ``heat_penalty`` lines (challenge id -> points), logged in ``round_scored.heat``; {} off ledger."""
    if rules.ledger is None:
        return {}
    return heat_lines(
        challenges, validations, attacks, round_no, rules.ledger, lambda ch: _defense(ch, round_no, rules.scoring)
    )


def heat_of_state(state: MatchState, round_no: int) -> dict[str, int]:
    """``round_heat`` for a folded match: what ``round_scored.heat`` records (and replay recomputes)."""
    attacks = {a.challenge_id: a for a in state.attacks.values()}
    return round_heat(state.challenges.values(), state.validations, attacks, round_no, state.rules)


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
            scores[atk.attacker] += solve_award(ch.spec, facts.round_no, max(1, atk.attempts), rule)
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
    _cool(facts, rule, scores)
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

    A stake of 0 means the attacker skipped the challenge, so it scores as a defense; a recorded capture on a
    0-stake challenge, or one with no stake, is a corrupt or tampered log and raises. Discount is 0..50%, never
    below 1, so defense >= capture (ADR 0006) still holds. A revealed keystone doubles its defense award, or costs
    its author ``base // 2`` when captured. Heat is a separate penalty
    line on the author (``facts.heat``, ADR 0006 addendum); it never touches the defense award.
    """
    _check_stakes(facts)
    scores = classic(facts, rule)
    _cool(facts, rule, scores)
    for attacker, classic_paid, paid in _captures(
        facts.challenges, facts.validations, facts.attacks, facts.round_no, rule
    ).values():
        scores[attacker] -= classic_paid - paid
    for author, cid in facts.keystones.items():  # revealed keystone: a defender-side bet (ADR 0006 holds)
        stone = next((c for c in facts.challenges if c.id == cid and c.author == author), None)
        ok = facts.validations.get(cid)
        if stone is None or stone.spec is None or ok is None or not ok.valid:
            continue  # an invalid keystone forfeits
        hit = facts.attacks.get(cid)
        if hit is not None and hit.solved:
            scores[author] -= solve_award(stone.spec, facts.round_no, max(1, hit.attempts), rule) // 2
        else:  # doubles what classic paid, so a probe-solved (halved) stone stays at 2x, not 3x
            scores[author] += _defense(stone, facts.round_no, rule)
    return scores


def _cool(facts: RoundFacts, rule: ScoringRule, scores: dict[str, int]) -> None:
    """Heat penalty lines: charged to the author, never credited to the opponent (ADR 0006 addendum)."""
    authors = {c.id: c.author for c in facts.challenges}
    for cid, pts in sorted(facts.heat.items()):
        if cid in authors:
            scores[authors[cid]] -= pts


def _defense(ch: Challenge, round_no: int, rule: ScoringRule) -> int:
    """What classic pays a defended challenge: halved when the build probe solved it."""
    assert ch.spec is not None
    award = defense_award(ch.spec, round_no, rule)
    return award // 2 if ch.probe_solved else award


def _check_stakes(facts: RoundFacts) -> None:
    ids = {c.id for c in facts.challenges}  # attacks may span every round; check only this one's
    for a in (a for a in facts.attacks.values() if a.challenge_id in ids):
        if a.ghost is not None:  # free capture: only real if the log's ghost_run hit it, and staked 0 (refunded)
            if not a.solved or facts.ghost_hits.get(a.challenge_id) != a.ghost:
                raise EventLogCorruptError(f"tampered: ghost capture on {a.challenge_id} has no matching ghost_run hit")
            if a.stake:
                raise EventLogCorruptError(f"tampered: ghost capture on {a.challenge_id} was staked {a.stake}, not 0")
            continue
        if a.solved and a.stake is None:
            raise EventLogCorruptError(f"ledger: solved attack on {a.challenge_id} has no stake")
        if a.solved and a.stake == 0:
            raise EventLogCorruptError(f"tampered: capture on {a.challenge_id}, which was staked 0 (skipped)")


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
