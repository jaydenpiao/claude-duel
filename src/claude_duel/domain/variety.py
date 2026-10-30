"""Puzzle variety for the check form (#79): a per-challenge theme, the author's own past designs, and a
repeat check. Pure and seeded, so replay and resume reproduce the same themes.
"""

import hashlib

from claude_duel.domain.contract import definitions
from claude_duel.domain.memory import technique
from claude_duel.domain.models import ChallengeSpec, MatchState

LOCK_AXIS = "lock puzzle"
# Design rec R2: families differ in what the answer *is*, not just in the checker. The old 8 themes are one axis.
TAXONOMY: dict[str, tuple[str, ...]] = {
    "combinatorial witness": (
        "a Latin-square row set (the input fills the missing cells)",
        "a proper colouring of a small graph stated in check()",
        "a Hamiltonian path through a small graph stated in check()",
        "a knight's-tour fragment on a small board",
    ),
    "program in a tiny stated language": (
        "a sorting network (the input is a comparator list that sorts every case)",
        "a turtle program that must trace a stated shape",
        "a pattern in a tiny regex-like language that must match one word set and reject another",
        "a register-machine program that must compute a stated value",
    ),
    "moves against a fixed deterministic bot": (
        "Nim against a bot with a fixed rule (the input is your move list)",
        "tic-tac-toe to a forced win against a fixed bot",
        "a sokoban push list on a small map",
        "a coin-taking game against a greedy bot",
    ),
    "classical cipher with a derivable key": (
        "a Vigenere decryption whose key is defined by stated properties",
        "an affine cipher whose key follows from stated facts",
        "a columnar transposition with a derivable column order",
        "a substitution cipher on a stated text with a derivable rule",
    ),
    "simulation": (
        "a cellular-automaton start row that reaches a target row after k steps",
        "integer ballistics (launch values that hit a stated target)",
        "an elevator or queue schedule that meets stated deadlines",
        "a bouncing-ball or reflection path that ends at a stated cell",
    ),
    "language and encoding": (
        "Roman-numeral arithmetic",
        "base-k arithmetic in a stated base",
        "calendar arithmetic (weekdays, day counts)",
        "a balanced-bracket word with stated counts",
        "a word ladder under stated letter rules",
    ),
    LOCK_AXIS: (
        "grid or maze walk (moves over a small map)",
        "tiny stack or register VM (the input is a program)",
        "digit constraints (sums, products, order, modulo)",
        "string recurrence (a key built by a loop)",
        "bit manipulation (xor, shifts, masks on char codes)",
        "state machine (transitions over input symbols)",
        "weighted checksum (each position has a weight; several sums must hold)",
        "modular arithmetic (the input encodes numbers that must satisfy congruences)",
    ),
}
THEMES = TAXONOMY[LOCK_AXIS]  # the pre-R2 theme list


def _ranked(seed: str, salt: str, items: tuple[str, ...] | list[str]) -> list[str]:
    return sorted(items, key=lambda t: hashlib.sha256(f"{seed}:{salt}:{t}".encode()).hexdigest())


def slot_axis(seed: str, slot: int) -> str:
    """The axis of challenge slot ``slot`` (0-based over the match): every axis once in a seeded order, then
    the non-lock axes on repeat, so a match has at most one lock puzzle per agent."""
    first = _ranked(seed, "axis", list(TAXONOMY))
    if slot < len(first):
        return first[slot]
    rest = [a for a in first if a != LOCK_AXIS]
    return rest[(slot - len(first)) % len(rest)]


def family(seed: str, agents: list[str], agent: str, round_no: int, index: int, per_round: int) -> tuple[str, str]:
    """(axis, family) for ``agent``'s challenge: both agents share the slot's axis but never its family."""
    slot = (round_no - 1) * per_round + index
    axis = slot_axis(seed, slot)
    visit = sum(slot_axis(seed, k) == axis for k in range(slot))
    pool = _ranked(seed, axis, TAXONOMY[axis])
    side = sorted(agents).index(agent) if agent in agents else 0
    return axis, pool[(len(agents) * visit + side) % len(pool)]


def theme(seed: str, agent: str, round_no: int, index: int, per_round: int) -> str:
    """Pre-R2 helper kept for old callers: a seeded lock-puzzle theme per agent challenge."""
    order = _ranked(seed, agent, THEMES)
    return order[((round_no - 1) * per_round + index) % len(order)]


def past_designs(state: MatchState, agent: str) -> list[ChallengeSpec]:
    """The agent's own earlier challenges (oldest first)."""
    mine = sorted((c for c in state.challenges.values() if c.author == agent and c.spec), key=lambda c: c.id)
    return [c.spec for c in mine if c.spec is not None]


def prompt_lines(state: MatchState, agent: str, index: int) -> list[str]:
    seed = state.seed or state.match_id
    names = [a.name for a in state.agents]
    axis, fam = family(seed, names, agent, state.round, index, state.rules.challenges_per_round)
    lines = [
        f"FAMILY for this challenge (assigned): {axis}: {fam}. Your opponent builds a different family from the "
        "same axis. Build this family, not a variant of another; never reuse an earlier design. Define the "
        "answer by properties check() tests, never by a target string or list in the source."
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
            return f"repeat design: you already used the title {old.title!r}; build a new puzzle in the assigned family"
        if _normal(old.source) == own:
            return "repeat design: this check is the same as an earlier one; build a new puzzle in the assigned family"
    return None


def _normal(source: str) -> str:
    return " ".join(definitions(source).split())
