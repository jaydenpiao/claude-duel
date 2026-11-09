"""Novelty heat (design-core-logic.md 4.3, ADR 0006 addendum): a separate defender-side penalty line.

Pure and computed from logged source only. Each source becomes a token sequence from a true pre-order walk of its
AST (node, then ``ast.iter_child_nodes`` in field order): the node type, plus the value type for ``Constant`` and
the operator class names for ``BinOp``/``UnaryOp``/``BoolOp``/``Compare``. Names, literal values, comments and
formatting are dropped. Similarity is ``100 * LCS // max(len)``, integer floor. A defended challenge whose best
similarity to earlier valid challenges of the last ``heat_window`` rounds (the author's own, and when
``LedgerRule.heat_vs_opponent`` the opponent's) is at least ``heat_threshold_pct`` gets a ``heat_penalty``
line; its defense award never changes.
"""

import ast
from collections.abc import Callable, Iterable, Mapping, Sequence

from claude_duel.domain.models import AgentSpec, Attack, Challenge, LedgerRule, MatchState, Validation

_SKIP = (
    ast.expr_context,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
)  # contexts; ops are emitted by their parent
DefenseFn = Callable[[Challenge], int]


def tokens(source: str) -> tuple[str, ...]:
    """Pre-order AST tokens; () when the source does not parse or is empty."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ()
    out: list[str] = []
    stack: list[ast.AST] = list(reversed(tree.body))  # the Module root is in every file, so it is not a token
    while stack:
        node = stack.pop()
        if isinstance(node, _SKIP):
            continue
        out.append(type(node).__name__)
        if isinstance(node, ast.Constant):
            out.append(type(node.value).__name__)
        elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp)):
            out.append(type(node.op).__name__)
        elif isinstance(node, ast.Compare):
            out.extend(type(op).__name__ for op in node.ops)
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return tuple(out)


def _lcs(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> int:
    """``100 * LCS // max(len)`` over the two token sequences, integer percent (0 when either is empty)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0
    return 100 * _lcs(ta, tb) // max(len(ta), len(tb))


def _valid(ch: Challenge, validations: Mapping[str, Validation]) -> bool:
    v = validations.get(ch.id)
    return ch.spec is not None and v is not None and v.valid


def heat(
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    round_no: int,
    rule: LedgerRule,
    refs: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, int]:
    """challenge id -> best similarity to the valid challenges of the last ``heat_window`` rounds (the author's own
    and, design R5, the opponent's) plus ``refs[author]`` (sources from the same model's last matches, frozen in
    ``match_created``), for this round's valid challenges at or over ``heat_threshold_pct`` only. Invalid ones
    never take or give heat."""
    if not rule.heat_window:
        return {}
    every = sorted((c for c in challenges if _valid(c, validations)), key=lambda c: c.id)
    out: dict[str, int] = {}
    for ch in (c for c in every if c.round == round_no):
        assert ch.spec is not None
        past = [
            c.spec.source
            for c in every
            if round_no - rule.heat_window <= c.round < round_no
            and c.spec is not None
            and (rule.heat_vs_opponent or c.author == ch.author)
        ]
        past += list((refs or {}).get(ch.author, ()))
        top = max((similarity(ch.spec.source, s) for s in past), default=0)
        if top >= rule.heat_threshold_pct:
            out[ch.id] = top
    return out


def penalty(defense: int, h: int, rule: LedgerRule) -> int:
    """``min(D * heat_max_pct // 100, D * (h - thr) // (100 - thr))``; 0 at the threshold, the cap at 100%."""
    thr = rule.heat_threshold_pct
    if h < thr:
        return 0
    ramp = defense * (h - thr) // (100 - thr) if thr < 100 else defense
    return min(defense * rule.heat_max_pct // 100, ramp)


def heat_lines(
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    attacks: Mapping[str, Attack],
    round_no: int,
    rule: LedgerRule,
    defense: DefenseFn,
    refs: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, int]:
    """challenge id -> its ``heat_penalty`` line (0 at the threshold), for defended heated challenges only.
    ``attacks`` is keyed by challenge id; ``defense(ch)`` is what classic paid that defense."""
    chs = list(challenges)
    hot = heat(chs, validations, round_no, rule, refs)
    out: dict[str, int] = {}
    for ch in chs:
        atk = attacks.get(ch.id)
        if ch.id in hot and not (atk is not None and atk.solved):
            out[ch.id] = penalty(defense(ch), hot[ch.id], rule)
    return out


def history_refs(past: Iterable[MatchState], agents: Iterable[AgentSpec], k: int = 3) -> dict[str, list[str]]:
    """agent name -> valid challenge sources its model (same provider and model) built in its last ``k`` matches,
    oldest first by ``created_seq``. Frozen into ``match_created`` so heat replays exactly."""
    ordered = sorted(past, key=lambda m: (m.created_seq is None, m.created_seq or 0, m.match_id))
    out: dict[str, list[str]] = {}
    for spec in agents:
        mine = [(m, a.name) for m in ordered for a in m.agents if (a.provider, a.model) == (spec.provider, spec.model)]
        sources = [
            c.spec.source
            for m, name in mine[-k:]
            for c in sorted(m.challenges.values(), key=lambda c: c.id)
            if c.author == name and _valid(c, m.validations) and c.spec is not None
        ]
        if sources:
            out[spec.name] = sources
    return out
