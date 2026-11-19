"""Puzzle variety for the check form (#79): a per-challenge theme, the author's own past designs, and a
repeat check. Pure and seeded, so replay and resume reproduce the same themes.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping

from claude_duel.domain.contract import definitions
from claude_duel.domain.memory import technique
from claude_duel.domain.models import AgentSpec, ChallengeSpec, MatchState
from claude_duel.domain.signature import SIGNATURES, signatures, tokens

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


def family_id(desc: str) -> str:
    """Short id a spec declares for a family description (R2 binding), e.g. "latin-square-row-set"."""
    words = re.sub(r"[^a-z0-9]+", " ", desc.split(" (", maxsplit=1)[0].casefold()).split()
    if words and words[0] in {"a", "an"}:
        words = words[1:]
    return "-".join(words[:5])


def _draws(
    seed: str, agents: list[str], slots: int, bans: Mapping[str, Iterable[str]] | None = None
) -> list[tuple[str, dict[str, str]]]:
    """(axis, agent -> family) for slots 0..slots-1. Pure: both agents share the axis but never a family, no
    agent repeats a family, and a family the agent built in recent matches (``bans``) is used only if nothing
    else is left."""
    order = sorted(agents)
    banned = {a: set((bans or {}).get(a, ())) for a in order}
    used: dict[str, set[str]] = {a: set() for a in order}
    visits: dict[str, int] = {}
    out: list[tuple[str, dict[str, str]]] = []
    for slot in range(slots):
        axis = slot_axis(seed, slot)
        visit = visits.get(axis, 0)
        visits[axis] = visit + 1
        pool = _ranked(seed, axis, TAXONOMY[axis])
        picks: dict[str, str] = {}
        for side, a in enumerate(order):
            start = (len(order) * visit + side) % len(pool)
            rotated = pool[start:] + pool[:start]
            free = [f for f in rotated if f not in used[a] and f not in picks.values()] or rotated
            pick = next((f for f in free if f not in banned[a]), free[0])
            picks[a] = pick
            used[a].add(pick)
        out.append((axis, picks))
    return out


def family(
    seed: str,
    agents: list[str],
    agent: str,
    round_no: int,
    index: int,
    per_round: int,
    bans: Mapping[str, Iterable[str]] | None = None,
) -> tuple[str, str]:
    """(axis, family) for ``agent``'s challenge: both agents share the slot's axis but never its family."""
    slot = (round_no - 1) * per_round + index
    axis, picks = _draws(seed, agents, slot + 1, bans)[slot]
    return axis, picks.get(agent, picks[sorted(agents)[0]])


def state_family(state: MatchState, agent: str, index: int) -> tuple[str, str]:
    names = [a.name for a in state.agents]
    seed = state.seed or state.match_id
    return family(seed, names, agent, state.round, index, state.rules.challenges_per_round, state.family_bans)


def drawn(state: MatchState) -> dict[str, list[str]]:
    """Every family each agent was assigned over the match's rounds (for later matches' bans)."""
    names = [a.name for a in state.agents]
    slots = state.rules.rounds * state.rules.challenges_per_round
    out: dict[str, list[str]] = {n: [] for n in names}
    for _, picks in _draws(state.seed or state.match_id, names, slots, state.family_bans):
        for n in names:
            out[n].append(picks[n])
    return out


def history_bans(past: Iterable[MatchState], agents: Iterable[AgentSpec], k: int = 3) -> dict[str, list[str]]:
    """R2 cross-match ban: per agent, the families the same model was assigned in its last ``k`` variety
    matches (by creation order). Frozen into ``match_created`` so replay never rereads the store."""
    ordered = sorted(
        (m for m in past if m.rules.variety), key=lambda m: (m.created_seq is None, m.created_seq or 0, m.match_id)
    )
    bans: dict[str, list[str]] = {}
    for spec in agents:
        key = (spec.provider, spec.model)
        mine = [(m, a.name) for m in ordered for a in m.agents if (a.provider, a.model) == key]
        fams = sorted({f for m, name in mine[-k:] for f in drawn(m).get(name, [])})
        if fams:
            bans[spec.name] = fams
    return bans


def family_reason(state: MatchState, agent: str, index: int, spec: ChallengeSpec) -> str | None:
    """R2 binding: a declared family must be the assigned one ("" is tolerated for old replies)."""
    axis, fam = state_family(state, agent, index)
    sigs = signatures(spec.source) if axis != LOCK_AXIS else []
    if sigs:
        shapes = ", ".join(f"{s} ({SIGNATURES[s]})" for s in sigs)
        return (
            f"wrong family: this check has a lock-puzzle structure [{shapes}] but the assigned family is "
            f"{family_id(fam)!r} ({fam}); build that family, not a lock"
        )
    if not spec.family.strip():
        return None
    want = family_id(fam)
    if spec.family.strip().casefold() != want:
        return f"wrong family: you declared {spec.family.strip()!r}; build the assigned family {want!r} ({fam})"
    return None


def theme(seed: str, agent: str, round_no: int, index: int, per_round: int) -> str:
    """Pre-R2 helper kept for old callers: a seeded lock-puzzle theme per agent challenge."""
    order = _ranked(seed, agent, THEMES)
    return order[((round_no - 1) * per_round + index) % len(order)]


def past_designs(state: MatchState, agent: str) -> list[ChallengeSpec]:
    """The agent's own earlier challenges (oldest first)."""
    mine = sorted((c for c in state.challenges.values() if c.author == agent and c.spec), key=lambda c: c.id)
    return [c.spec for c in mine if c.spec is not None]


def prompt_lines(state: MatchState, agent: str, index: int) -> list[str]:
    axis, fam = state_family(state, agent, index)
    lines = [
        f'FAMILY for this challenge (assigned): {axis}: {fam}; set "family": "{family_id(fam)}". '
        "Your opponent builds a different family from the "
        "same axis. Build this family, not a variant of another; never reuse an earlier design. Define the "
        "answer by properties check() tests, never by a target string or list in the source."
    ]
    example = AXIS_EXAMPLES.get(axis)
    if example is not None and state.rules.challenge_form == "check":
        lines.append(
            f"One example from this axis (format and level only; build your own family): {json.dumps(example)}"
        )
    past = past_designs(state, agent)
    if past:
        listed = "; ".join(f"{s.title!r} [{technique(s.source)}]" for s in past)
        lines.append(f"Your past designs (a repeat title or source is rejected): {listed}")
    return lines


def repeat_reason(state: MatchState, agent: str, spec: ChallengeSpec) -> str | None:
    """Why ``spec`` repeats one of the agent's earlier designs, or None."""
    own, shape = _normal(spec.source), tokens(spec.source)
    for old in past_designs(state, agent):
        if old.title.strip().casefold() == spec.title.strip().casefold():
            return f"repeat design: you already used the title {old.title!r}; build a new puzzle in the assigned family"
        if _normal(old.source) == own or (shape and tokens(old.source) == shape):
            return "repeat design: this check is the same as an earlier one; build a new puzzle in the assigned family"
    return None


def _normal(source: str) -> str:
    return " ".join(definitions(source).split())


def _ex(axis: str, n: int, title: str, description: str, source: str, solver: str) -> dict[str, object]:
    return {
        "title": title,
        "description": description,
        "source": source,
        "solver": solver,
        "reference_payload": "",
        "difficulty": 2,
        "family": family_id(TAXONOMY[axis][n]),
    }


# R2 few-shot pool: one vetted example per axis (tests run each through the checked arena, R1 included).
AXIS_EXAMPLES: dict[str, dict[str, object]] = {
    "combinatorial witness": _ex(
        "combinatorial witness",
        1,
        "Three-colour the graph",
        "Colour 6 nodes with 0/1/2 so no edge joins equal colours; node 0 is 0 and node 1 is 1.",
        "EDGES = [(0, 1), (1, 2), (2, 0), (2, 3), (3, 4), (4, 5), (5, 3), (1, 4)]\n\n\n"
        "def check(s):\n    if len(s) != 6 or any(c not in '012' for c in s):\n        return False\n"
        "    if s[0] != '0' or s[1] != '1':\n        return False\n"
        "    return all(s[a] != s[b] for a, b in EDGES)\n",
        "cols = {}\nfor v in range(6):\n    for c in '012':\n"
        "        if all(cols.get(u) != c for a, b in EDGES for u in (a, b) if v in (a, b) and u != v):\n"
        "            cols[v] = c\n            break\nprint(''.join(cols[v] for v in range(6)))\n",
    ),
    "program in a tiny stated language": _ex(
        "program in a tiny stated language",
        3,
        "Inc and double",
        "A register starts at 0; i adds 1, d doubles it. At most 8 ops must leave exactly 37.",
        "def check(s):\n    if not 0 < len(s) <= 8 or any(c not in 'id' for c in s):\n        return False\n"
        "    r = 0\n    for c in s:\n        r = r + 1 if c == 'i' else r * 2\n    return r == 37\n",
        "ops = ''\nfor b in bin(37)[2:]:\n    ops += 'd' if ops else ''\n"
        "    ops += 'i' if b == '1' else ''\nprint(ops)\n",
    ),
    "moves against a fixed deterministic bot": _ex(
        "moves against a fixed deterministic bot",
        0,
        "Nim from 21",
        "One pile of 21; take 1-3 each turn, you first. The bot takes pile % 4 (or 1). Take the last one.",
        "def bot(p):\n    return p % 4 or 1\n\n\n"
        "def check(s):\n    p = 21\n    for c in s:\n"
        "        if c not in '123' or int(c) > p:\n            return False\n"
        "        p -= int(c)\n        if p == 0:\n            return True\n        p -= bot(p)\n"
        "        if p <= 0:\n            return False\n    return False\n",
        "p, out = 21, ''\nwhile p:\n    m = p % 4 or 1\n    out += str(m)\n    p -= m\n    if p:\n        p -= bot(p)\n"
        "print(out)\n",
    ),
    "classical cipher with a derivable key": _ex(
        "classical cipher with a derivable key",
        1,
        "Affine key",
        "Give a,b as two 2-digit numbers (aabb) so E(x) = (a*x + b) % 26 turns ARENA into IPCVI.",
        "def enc(a, b, w):\n    return ''.join(chr(65 + (a * (ord(c) - 65) + b) % 26) for c in w)\n\n\n"
        "def check(s):\n    if len(s) != 4 or not s.isdigit():\n        return False\n"
        "    a, b = int(s[:2]), int(s[2:])\n    return a < 26 and b < 26 and a % 2 == 1 and a != 13 "
        "and enc(a, b, 'ARENA') == 'IPCVI'\n",
        "b = ord('I') - 65\na = next(x for x in range(1, 26, 2) if (17 * x + b) % 26 == ord('P') - 65)\n"
        "print(f'{a:02d}{b:02d}')\n",
    ),
    "simulation": _ex(
        "simulation",
        0,
        "Rule 90 preimage",
        "8 wrapped cells (0/1). Each step a cell becomes left XOR right. After 3 steps all cells are 1.",
        "def step(r):\n    return [r[i - 1] ^ r[(i + 1) % len(r)] for i in range(len(r))]\n\n\n"
        "def check(s):\n    if len(s) != 8 or any(c not in '01' for c in s):\n        return False\n"
        "    r = [int(c) for c in s]\n    for _ in range(3):\n        r = step(r)\n    return all(r)\n",
        "for n in range(256):\n    s = bin(n)[2:].zfill(8)\n    if check(s):\n        print(s)\n        break\n",
    ),
    "language and encoding": _ex(
        "language and encoding",
        0,
        "Well-formed Roman",
        "The canonical Roman numeral for the year when 1000 + 987 have passed.",
        "VALS = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'), (90, 'XC'), (50, 'L'), "
        "(40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]\n\n\n"
        "def roman(n):\n    out = ''\n    for v, t in VALS:\n        while n >= v:\n            out += t\n"
        "            n -= v\n    return out\n\n\n"
        "def check(s):\n    return 0 < len(s) < 20 and roman(1000 + 987) == s\n",
        "print(roman(1000 + 987))\n",
    ),
    LOCK_AXIS: _ex(
        LOCK_AXIS,
        3,
        "Rolling key",
        "The key is generated by a small recurrence.",
        "def key():\n    x, out = 7, ''\n    for i in range(6):\n        x = (x * 31 + 11 + i) % 26\n"
        "        out += chr(65 + x)\n    return out\n\n\ndef check(s):\n    return s == key()\n",
        "print(key())\n",
    ),
}
