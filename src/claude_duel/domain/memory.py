"""Between-round memory (#19, design-memory-strategy): a pure, bounded, flag-free summary of past rounds.

Built only from event-sourced state, so replay reproduces it. Shows each agent its own outcomes, the
opponent's past challenges (title, outcome, technique tag and source, which was public during the attack
anyway), opponent tendencies and the scoreboard. Never ``solver`` or ``reference_payload``.
"""

import ast
from collections import Counter

from claude_duel.domain.flags import redact
from claude_duel.domain.heat import heat
from claude_duel.domain.models import Attack, Challenge, MatchState, MemoryRule
from claude_duel.domain.scoring import heat_of_state
from claude_duel.domain.stakes import keystone_targets

OMITTED = "[older rounds omitted]"
PAYLOAD_CHARS = 120
NOTE_CHARS = 500
_TAGS = (  # first match wins; names seen anywhere in the source
    ("hash-check", {"hashlib"}),
    ("vm", {"stack", "pop", "ip", "pc", "ops", "program"}),
    ("maze", {"grid", "maze", "board"}),
    ("state-machine", {"state", "transitions"}),
    ("checksum", {"ord", "sum", "crc32", "checksum"}),
)


def technique(source: str) -> str:
    """Static, deterministic tag of a challenge's technique."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "other"
    names = {_name(n) for n in ast.walk(tree)}
    return next((tag for tag, keys in _TAGS if names & keys), "other")


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.alias):  # import targets
        return node.name
    return node.attr if isinstance(node, ast.Attribute) else ""


def summarize(state: MatchState, agent: str, upto_round: int, rule: MemoryRule) -> str:
    """Memory for ``agent`` after rounds 1..upto_round, at most ``rule.max_chars``; oldest rounds drop first."""
    if rule.kind == "none" or upto_round < 1:
        return ""
    opponent = state.opponent(agent)
    by_challenge = {a.challenge_id: a for a in state.attacks.values()}
    rounds = [_round_block(state, r, agent, by_challenge, rule) for r in range(1, upto_round + 1)]
    tail = [_tendencies(state, opponent, upto_round, by_challenge), f"Scoreboard: {_board(state)}"]
    if rule.kind == "summary+scratchpad":
        tail.append(_scratchpad(state, agent, upto_round))
    dropped = False
    while True:
        text = "\n".join([*([OMITTED] if dropped else []), *rounds, *tail])
        if len(text) <= rule.max_chars or not rounds:
            return text[: rule.max_chars]
        rounds, dropped = rounds[1:], True


def _round_block(state: MatchState, r: int, agent: str, attacks: dict[str, Attack], rule: MemoryRule) -> str:
    lines = [f"Round {r}:"]
    stones = set(keystone_targets(state, r).values())  # revealed only, so past rounds never leak a sealed pick
    hot = heat(state.challenges.values(), state.validations, r, state.rules.ledger) if state.rules.ledger else {}
    lines_ = heat_of_state(state, r) if r in state.scored_rounds else {}
    for ch in sorted(state.round_challenges(r), key=lambda c: c.id):
        atk = attacks.get(ch.id)
        mark = " (keystone)" if ch.id in stones else ""
        if ch.author == agent:
            warm = f" (heat {hot[ch.id]}%, penalty {lines_.get(ch.id, 0)})" if ch.id in hot else ""
            lines.append(f"- yours{mark} {_title(ch)}: {_outcome(state, ch, atk, yours=True)}{warm}")
        else:
            lines.append(f"- theirs{mark} {_title(ch)}: {_outcome(state, ch, atk, yours=False)}")
            if ch.spec is not None:
                source = redact(ch.spec.source)
                cut = source[: rule.source_chars] + ("\n…" if len(source) > rule.source_chars else "")
                lines.append(f"  source:\n{cut}")
    return "\n".join(lines)


def _title(ch: Challenge) -> str:
    if ch.spec is None:
        return "(forfeit)"
    return f"{redact(ch.spec.title)[:60]!r} d{ch.spec.difficulty} [{technique(ch.spec.source)}]"


def _outcome(state: MatchState, ch: Challenge, atk: Attack | None, *, yours: bool) -> str:
    v = state.validations.get(ch.id)
    if ch.spec is None or v is None or not v.valid:
        return f"invalid: {redact(v.reason if v else ch.forfeit_reason or 'forfeit')[:80]}"
    if atk is None:
        return "not attacked"
    if not atk.solved:
        return f"held after {atk.attempts} tries"
    last = state.attempts.get(f"{ch.id}:{atk.attacker}", [])
    payload = redact(last[-1].payload)[:PAYLOAD_CHARS] if last else "?"
    who = "they captured it" if yours else "you captured it"
    return f"{who} on try {atk.attempts} with {payload!r}"


def _tendencies(state: MatchState, opponent: str, upto: int, attacks: dict[str, Attack]) -> str:
    tags = Counter(
        technique(c.spec.source)
        for c in state.challenges.values()
        if c.spec is not None and c.author == opponent and c.round <= upto
    )
    solves = [a.attempts for a in attacks.values() if a.attacker == opponent and a.solved]
    pace = f"{sum(solves) / len(solves):.1f} tries per capture" if solves else "no captures yet"
    return f"Opponent builds: {dict(sorted(tags.items())) or 'none'}; opponent attacks: {pace}"


def _scratchpad(state: MatchState, agent: str, upto: int) -> str:
    notes = state.notes.get(agent, {})
    last = max((r for r in notes if r <= upto), default=None)
    if last is None:
        return "Your scratchpad: (empty)"
    return f"Your scratchpad (round {last}, private):\n{redact(notes[last])[:NOTE_CHARS]}"


def _board(state: MatchState) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(state.scores.items())) or "0-0"
