"""Between-round memory (#19, design-memory-strategy): a pure, bounded, flag-free summary of past rounds.

Built only from event-sourced state, so replay reproduces it. Shows each agent its own outcomes, the
opponent's past challenges (title, outcome, technique tag and, once that round is scored, source, which
was public during the attack anyway), opponent tendencies and the scoreboard. Never ``solver`` or
``reference_payload``.

The size is a deterministic token budget: ``est_tokens`` (4 UTF-8 bytes per token, no vendor tokenizer)
must stay within ``MemoryRule.max_tokens`` for the block and ``source_tokens`` for all sources together.
Each revealed source gets ``source_tokens // n`` (at least 64) and is cut at a line boundary. Over the
block budget, drop: oldest rounds, then tendencies, then your last payloads, then sources (oldest first).
The scoreboard and the latest round's outcomes are kept.
"""

import ast
import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace

from claude_duel.domain.flags import redact
from claude_duel.domain.models import Attack, Challenge, MatchState, MemoryRule

OMITTED = "[older rounds omitted]"
SOURCE_OMITTED = "# [source omitted: budget]"
PAYLOAD_CHARS = 120
NOTE_CHARS = 500
MIN_SOURCE_SHARE = 64
_TAGS = (  # first match wins; names seen anywhere in the source
    ("hash-check", {"hashlib"}),
    ("vm", {"stack", "pop", "ip", "pc", "ops", "program"}),
    ("maze", {"grid", "maze", "board"}),
    ("state-machine", {"state", "transitions"}),
    ("checksum", {"ord", "sum", "crc32", "checksum"}),
)


def est_tokens(text: str) -> int:
    """Vendor-independent token estimate: ceil(UTF-8 bytes / 4)."""
    return math.ceil(len(text.encode("utf-8")) / 4)


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


@dataclass(frozen=True)
class _Entry:
    round: int
    line: str
    payload: str = ""  # your last payload on their challenge (droppable)
    source: str | None = None  # revealed, already cut to its share


@dataclass(frozen=True)
class _View:
    rounds: tuple[int, ...]
    tendencies: bool = True
    payloads: bool = True
    sources: tuple[int, ...] = ()  # rounds whose sources are still shown
    dropped: bool = False


def summarize(state: MatchState, agent: str, upto_round: int, rule: MemoryRule) -> str:
    """Memory for ``agent`` after rounds 1..upto_round, within ``rule.max_tokens`` (see module doc)."""
    if rule.kind == "none" or upto_round < 1:
        return ""
    by_challenge = {a.challenge_id: a for a in state.attacks.values()}
    sources = cut_sources(state, agent, upto_round, rule)
    entries = [_entry(state, ch, agent, by_challenge, sources) for ch in _challenges(state, upto_round)]
    tail = f"Scoreboard: {_board(state)}"
    if rule.kind == "summary+scratchpad":
        tail += "\n" + _scratchpad(state, agent, upto_round)
    tend = _tendencies(state, state.opponent(agent), upto_round, by_challenge)
    rounds = tuple(range(upto_round, 0, -1))  # newest first
    text = ""
    for view in _views(rounds):
        text = _render(entries, view, tend, tail)
        if est_tokens(text) <= rule.max_tokens:
            return text
    return _fit(text, rule.max_tokens)


def _views(rounds: tuple[int, ...]) -> Iterator[_View]:
    """The stated drop order: everything, then fewer old rounds, tendencies, payloads, sources oldest first."""
    view = _View(rounds=rounds, sources=rounds)
    yield view
    for n in range(1, len(rounds)):
        view = replace(view, rounds=rounds[: len(rounds) - n], dropped=True)
        yield view
    view = replace(view, tendencies=False)
    yield view
    view = replace(view, payloads=False)
    yield view
    for n in range(1, len(rounds) + 1):
        view = replace(view, sources=rounds[: len(rounds) - n])
        yield view


def _render(entries: list[_Entry], view: _View, tend: str, tail: str) -> str:
    lines = [OMITTED] if view.dropped else []
    for r in view.rounds:
        lines.append(f"Round {r}:")
        for e in (e for e in entries if e.round == r):
            lines.append(e.line + (e.payload if view.payloads else ""))
            if e.source is not None and r in view.sources:
                lines.append(f"  source:\n{e.source}")
    if view.tendencies:
        lines.append(tend)
    lines.append(tail)
    return "\n".join(lines)


def _fit(text: str, max_tokens: int) -> str:
    """Last resort (tiny budgets): keep whole leading lines that fit, never splitting a code point."""
    kept: list[str] = []
    for line in text.splitlines():
        if est_tokens("\n".join([*kept, line])) > max_tokens:
            break
        kept.append(line)
    return "\n".join(kept)


def _challenges(state: MatchState, upto: int) -> list[Challenge]:
    return sorted((c for c in state.challenges.values() if 1 <= c.round <= upto), key=lambda c: (-c.round, c.id))


def revealed(state: MatchState, agent: str, upto: int) -> list[Challenge]:
    """Opponent challenges whose source may be shown: their round is scored and before the current one."""
    return [
        c
        for c in _challenges(state, upto)
        if c.author != agent and c.spec is not None and c.round in state.scored_rounds
    ]


def cut_sources(state: MatchState, agent: str, upto: int, rule: MemoryRule) -> dict[str, str]:
    shown = revealed(state, agent, upto)
    if not shown:
        return {}
    share, left, out = max(MIN_SOURCE_SHARE, rule.source_tokens // len(shown)), rule.source_tokens, {}
    for ch in shown:
        assert ch.spec is not None
        body, note = cut_lines(redact(ch.spec.source), min(share, left))
        left -= est_tokens(body) if body else 0
        out[ch.id] = "\n".join(p for p in (body, note) if p) if body else SOURCE_OMITTED
    return out


def cut_lines(source: str, budget: int) -> tuple[str, str]:
    """The first whole lines within ``budget`` tokens, plus a truncation marker if any were cut."""
    lines = source.splitlines()
    kept: list[str] = []
    for line in lines:
        if est_tokens("\n".join([*kept, line])) > budget:
            break
        kept.append(line)
    note = f"# [truncated: {len(kept)} of {len(lines)} lines shown]" if kept and len(kept) < len(lines) else ""
    return "\n".join(kept), note


def _entry(state: MatchState, ch: Challenge, agent: str, attacks: dict[str, Attack], sources: dict[str, str]) -> _Entry:
    atk = attacks.get(ch.id)
    yours = ch.author == agent
    base, payload = _outcome(state, ch, atk, yours=yours)
    if yours:
        return _Entry(ch.round, f"- yours {_title(ch)}: {base}{payload}")
    return _Entry(ch.round, f"- theirs {_title(ch)}: {base}", payload, sources.get(ch.id))


def _title(ch: Challenge) -> str:
    if ch.spec is None:
        return "(forfeit)"
    return f"{redact(ch.spec.title)[:60]!r} d{ch.spec.difficulty} [{technique(ch.spec.source)}]"


def _outcome(state: MatchState, ch: Challenge, atk: Attack | None, *, yours: bool) -> tuple[str, str]:
    v = state.validations.get(ch.id)
    if ch.spec is None or v is None or not v.valid:
        return f"invalid: {redact(v.reason if v else ch.forfeit_reason or 'forfeit')[:80]}", ""
    if atk is None:
        return "not attacked", ""
    if not atk.solved:
        return f"held after {atk.attempts} tries", ""
    last = state.attempts.get(f"{ch.id}:{atk.attacker}", [])
    payload = redact(last[-1].payload)[:PAYLOAD_CHARS] if last else "?"
    who = "they captured it" if yours else "you captured it"
    return f"{who} on try {atk.attempts}", f" with {payload!r}"


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
