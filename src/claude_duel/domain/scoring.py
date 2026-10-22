"""Deterministic, pure scoring."""

from collections.abc import Iterable, Mapping

from claude_duel.domain.models import Attack, Challenge, Rules, Validation


def score_round(
    round_no: int,
    agents: Iterable[str],
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    attacks: Mapping[str, Attack],
    rules: Rules,
) -> dict[str, int]:
    scores = dict.fromkeys(agents, 0)
    by_challenge = {a.challenge_id: a for a in attacks.values()}
    for ch in sorted(challenges, key=lambda c: c.id):
        if ch.round != round_no:
            continue
        v = validations.get(ch.id)
        if ch.spec is None or v is None or not v.valid:
            scores[ch.author] -= rules.invalid_penalty * round_no
            continue
        weight = ch.spec.difficulty * round_no
        atk = by_challenge.get(ch.id)
        if atk is not None and atk.solved:
            scores[atk.attacker] += max(1, rules.solve_points * weight - (atk.attempts - 1))
        else:
            scores[ch.author] += rules.defense_points * weight
    return scores


def add_scores(total: Mapping[str, int], delta: Mapping[str, int]) -> dict[str, int]:
    keys = sorted(set(total) | set(delta))
    return {k: total.get(k, 0) + delta.get(k, 0) for k in keys}
