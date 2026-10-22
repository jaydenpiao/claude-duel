"""Puzzle variety for the check form (#79): a per-challenge theme, the author's own past designs, and a
repeat check. Pure and seeded, so replay and resume reproduce the same themes.
"""

import hashlib

from claude_duel.domain.contract import definitions
from claude_duel.domain.memory import technique
from claude_duel.domain.models import ChallengeSpec, MatchState

THEMES = (
    "grid or maze walk (moves over a small map)",
    "tiny stack or register VM (the input is a program)",
    "digit constraints (sums, products, order, modulo)",
    "string recurrence (a key built by a loop)",
    "bit manipulation (xor, shifts, masks on char codes)",
    "state machine (transitions over input symbols)",
    "weighted checksum (each position has a weight; several sums must hold)",
    "modular arithmetic (the input encodes numbers that must satisfy congruences)",
)


def theme(seed: str, agent: str, round_no: int, index: int, per_round: int) -> str:
    """A different theme for each of an agent's challenges, in a seeded order, until the list wraps."""
    order = sorted(THEMES, key=lambda t: hashlib.sha256(f"{seed}:{agent}:{t}".encode()).hexdigest())
    return order[((round_no - 1) * per_round + index) % len(order)]


def past_designs(state: MatchState, agent: str) -> list[ChallengeSpec]:
    """The agent's own earlier challenges (oldest first)."""
    mine = sorted((c for c in state.challenges.values() if c.author == agent and c.spec), key=lambda c: c.id)
    return [c.spec for c in mine if c.spec is not None]


def prompt_lines(state: MatchState, agent: str, index: int) -> list[str]:
    seed = state.seed or state.match_id
    lines = [
        f"THEME for this challenge: {theme(seed, agent, state.round, index, state.rules.challenges_per_round)}. "
        "Build that kind of puzzle; do not reuse an earlier design. Define the answer by properties check() "
        "tests, never by a target string or list in the source."
    ]
    past = past_designs(state, agent)
    if past:
        listed = "; ".join(f"{s.title!r} [{technique(s.source)}]" for s in past)
        lines.append(f"Your past designs (a repeat title or source is rejected): {listed}")
    return lines


def repeat_reason(state: MatchState, agent: str, spec: ChallengeSpec) -> str | None:
    """Why ``spec`` repeats one of the agent's earlier designs, or None."""
    own = _normal(spec.source)
    for old in past_designs(state, agent):
        if old.title.strip().casefold() == spec.title.strip().casefold():
            return f"repeat design: you already used the title {old.title!r}; build a new puzzle on the theme"
        if _normal(old.source) == own:
            return "repeat design: this check is the same as an earlier one; build a new puzzle on the theme"
    return None


def _normal(source: str) -> str:
    return " ".join(definitions(source).split())
