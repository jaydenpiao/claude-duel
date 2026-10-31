"""Prophecy glue over the match log (design-prophecy-wiring.md 2.2, 4): pure, no engine, no sandbox, $0."""

from __future__ import annotations

from claude_duel.domain import ghosts, prophecy
from claude_duel.domain.memory import technique
from claude_duel.domain.models import Challenge, MatchState, ProphecyRule
from claude_duel.domain.scoring import paid_defense
from claude_duel.domain.stakes import keystone_targets, own_challenges


def _valid(state: MatchState, target: str, round_no: int) -> list[Challenge]:
    """The target's valid challenges of ``round_no`` in stake (sorted id) order."""
    return [
        c
        for c in own_challenges(state.challenges, round_no, target)
        if c.spec is not None and (v := state.validations.get(c.id)) is not None and v.valid
    ]


def artifacts_of(state: MatchState, target: str, round_no: int) -> prophecy.RoundArtifacts | None:
    """What a prophecy about ``target`` in ``round_no`` settles on; None = void (no valid challenge)."""
    valid = _valid(state, target, round_no)
    if not valid:
        return None
    hits = ghosts.round_hits(state, round_no)
    own = {c.id for c in own_challenges(state.challenges, round_no, target)}
    in_round = {c.id for c in state.challenges.values() if c.round == round_no}
    captures = sum(
        1
        for a in state.attacks.values()
        if a.attacker == target and a.solved and a.ghost is None and a.challenge_id in in_round
    )
    return prophecy.RoundArtifacts(
        challenges=tuple(
            prophecy.Challenge(cid=c.id, size=len(c.spec.source.encode()), tag=technique(c.spec.source))
            for c in valid
            if c.spec is not None
        ),
        keystone=keystone_targets(state, round_no).get(target),
        captures=captures,
        ghost_kills=sum(1 for cid in hits if cid in own),
        stakes=tuple(state.stakes.get(f"{round_no}:{target}", ())),
    )


def round_cap(state: MatchState, target: str, round_no: int) -> int:
    """The rule's cap, lowered to the target's cheapest defense award (never below 1)."""
    rule = (state.rules.deep.prophecy if state.rules.deep else None) or ProphecyRule()
    awards = [paid_defense(c, round_no, state.rules.scoring) for c in _valid(state, target, round_no)]
    return prophecy.round_cap(rule.round_cap, awards)
