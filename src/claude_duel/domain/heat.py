"""Novelty heat (design-core-logic.md 4.3, ADR 0006 addendum): a separate defender-side penalty line.

Pure and computed from logged source only. Each source becomes a token sequence from a true pre-order walk of its
AST (node, then ``ast.iter_child_nodes`` in field order): the node type, plus the value type for ``Constant`` and
the operator class names for ``BinOp``/``UnaryOp``/``BoolOp``/``Compare``. Names, literal values, comments and
formatting are dropped. Similarity is ``100 * LCS // max(len)``, integer floor.

The LCS is quadratic, so it runs on the host only inside a fixed ceiling. A match that pins
``heat_token_cap`` / ``heat_ref_cap`` compares the token prefix and at most that many references (the in-match
window first, then the newest cross-match sources). A log without those pins — scored by the uncapped walk — is
recomputed exactly when every compared source still fits the ceiling, and raises ``HeatBoundError`` when it does
not, so replay reports the round unverified instead of substituting a different similarity. Sources over
``PARSE_BYTES`` are not parsed. A walk that the parser rejects (including deep nesting) is empty, the same as a
syntax error. One heat call compares at most ``MAX_CHALLENGES * REF_CAP`` pairs of at most ``TOKEN_CAP`` tokens
(``CELL_BUDGET`` cell updates). Repeated calls with the real similarity function reuse a bounded memo.
"""

from __future__ import annotations

import ast
import hashlib
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence

from claude_duel.domain.errors import HeatBoundError
from claude_duel.domain.models import AgentSpec, Attack, Challenge, LedgerRule, MatchState, Rules, Validation

PARSE_BYTES = 65_536
TOKEN_CAP = 256
REF_CAP = 48
MAX_CHALLENGES = 20  # two agents times challenges_per_round's maximum
CELL_BUDGET = MAX_CHALLENGES * REF_CAP * TOKEN_CAP * TOKEN_CAP
_MEMO_MAX = 64
_TOKEN_MAX = 512

_SKIP = (
    ast.expr_context,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
)  # contexts; ops are emitted by their parent
DefenseFn = Callable[[Challenge], int]
_Plan = tuple[tuple[str, str, tuple[str, ...]], ...]  # challenge id, its source, reference sources


class _Work:
    cells = 0


_MEMO: OrderedDict[tuple[object, ...], dict[str, int]] = OrderedDict()
_TOKENS: OrderedDict[tuple[bytes, int], tuple[str, ...]] = OrderedDict()


def work_cells() -> int:
    """DP cell updates performed by ``_lcs`` since the last ``reset_heat_cache`` (tests and the work bound)."""
    return _Work.cells


def reset_heat_cache() -> None:
    """Drop the memo and the token cache and zero the cell counter."""
    _Work.cells = 0
    _MEMO.clear()
    _TOKENS.clear()


def _raw(source: str) -> bytes | None:
    """UTF-8 bytes to parse, or None when the source is over the parse ceiling (it is not encoded past that)."""
    if len(source) > PARSE_BYTES:
        return None
    raw = source.encode()
    return None if len(raw) > PARSE_BYTES else raw


def _walk(source: str, limit: int) -> tuple[str, ...]:
    """First ``limit`` pre-order tokens, or () when the source does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ()
    out: list[str] = []
    stack: list[ast.AST] = list(reversed(tree.body))  # the Module root is in every file, so it is not a token
    while stack:
        if len(out) >= limit:
            break
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


def _tokens(source: str, limit: int) -> tuple[str, ...] | None:
    """Prefix of ``limit`` tokens, None when the source is over ``PARSE_BYTES`` (cached; the source is not retained)."""
    raw = _raw(source)
    if raw is None:
        return None
    key = (hashlib.sha256(raw).digest(), limit)
    hit = _TOKENS.get(key)
    if hit is not None:
        _TOKENS.move_to_end(key)
        return hit
    got = _walk(source, limit)
    _TOKENS[key] = got
    while len(_TOKENS) > _TOKEN_MAX:
        _TOKENS.popitem(last=False)
    return got


def tokens(source: str) -> tuple[str, ...]:
    """Pre-order AST tokens, at most ``TOKEN_CAP``.

    () when the source does not parse, is empty, or is over the parse ceiling.
    """
    got = _tokens(source, TOKEN_CAP)
    return () if got is None else got


def _lcs(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """LCS length. Refuses a pair past ``TOKEN_CAP`` before the quadratic loop."""
    if len(a) > TOKEN_CAP or len(b) > TOKEN_CAP:
        raise HeatBoundError()
    _Work.cells += len(a) * len(b)
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str, *, token_cap: int | None = None) -> int:
    """``100 * LCS // max(len)`` over token prefixes, integer percent (0 when either side is empty).

    ``token_cap`` set: compare the prefixes of that length (clamped to ``TOKEN_CAP``). ``token_cap`` None: the
    uncapped legacy percentage, but only when both sequences fit in ``TOKEN_CAP``; otherwise ``HeatBoundError``
    and no LCS. A source over the parse ceiling is empty when pinned and refused when legacy.
    """
    pinned = token_cap is not None
    limit = TOKEN_CAP if token_cap is None else min(token_cap, TOKEN_CAP)
    ta = _tokens(a, limit if pinned else limit + 1)
    tb = _tokens(b, limit if pinned else limit + 1)
    if ta is None or tb is None or (not pinned and (len(ta) > limit or len(tb) > limit)):
        if pinned:
            return 0
        raise HeatBoundError()
    ta, tb = ta[:limit], tb[:limit]
    if not ta or not tb:
        return 0
    return 100 * _lcs(ta, tb) // max(len(ta), len(tb))


def _valid(ch: Challenge, validations: Mapping[str, Validation]) -> bool:
    v = validations.get(ch.id)
    return ch.spec is not None and v is not None and v.valid


def _source(ch: Challenge) -> str:
    assert ch.spec is not None
    return ch.spec.source


def pinned_bound(rule: LedgerRule) -> tuple[int, int] | None:
    """``(token_cap, ref_cap)`` clamped to the ceiling, or None when the log did not pin either cap."""
    if rule.heat_token_cap is None and rule.heat_ref_cap is None:
        return None
    token = TOKEN_CAP if rule.heat_token_cap is None else min(rule.heat_token_cap, TOKEN_CAP)
    ref = REF_CAP if rule.heat_ref_cap is None else min(rule.heat_ref_cap, REF_CAP)
    return token, ref


def pin_heat_bound(rules: Rules) -> Rules:
    """Pin the ceiling on a new match that uses heat, clamping anything above it. Heat off is unchanged."""
    led = rules.ledger
    if led is None or not led.heat_window:
        return rules
    token = TOKEN_CAP if led.heat_token_cap is None else min(led.heat_token_cap, TOKEN_CAP)
    ref = REF_CAP if led.heat_ref_cap is None else min(led.heat_ref_cap, REF_CAP)
    if (led.heat_token_cap, led.heat_ref_cap) == (token, ref):
        return rules
    return rules.model_copy(update={"ledger": led.model_copy(update={"heat_token_cap": token, "heat_ref_cap": ref})})


def _window(every: Sequence[Challenge], round_no: int, rule: LedgerRule) -> list[Challenge]:
    return [c for c in every if c.spec is not None and round_no - rule.heat_window <= c.round < round_no]


def _references(window: list[Challenge], history: Sequence[str], rule: LedgerRule) -> list[str]:
    """References for one challenge. Pinned: the window first (newest rounds if it overflows), then newest history.
    Legacy: the whole set, or ``HeatBoundError`` when it is larger than ``REF_CAP``."""
    hist = list(history)
    bound = pinned_bound(rule)
    if bound is None:
        if len(window) + len(hist) > REF_CAP:
            raise HeatBoundError()
        return [_source(c) for c in window] + hist
    ref_cap = bound[1]
    chosen = window
    if len(chosen) > ref_cap:
        chosen = sorted(chosen, key=lambda c: (c.round, c.id))[-ref_cap:]
        chosen = sorted(chosen, key=lambda c: c.id)
    sources = [_source(c) for c in chosen]
    room = ref_cap - len(sources)
    return sources + (hist[-room:] if room else [])


def _plan(
    challenges: Iterable[Challenge],
    validations: Mapping[str, Validation],
    round_no: int,
    rule: LedgerRule,
    refs: Mapping[str, Sequence[str]] | None,
) -> _Plan:
    every = sorted((c for c in challenges if _valid(c, validations)), key=lambda c: c.id)
    current = [c for c in every if c.round == round_no]
    if len(current) > MAX_CHALLENGES:
        raise HeatBoundError()
    window = _window(every, round_no, rule)
    history = refs or {}
    return tuple((ch.id, _source(ch), tuple(_references(window, history.get(ch.author, ()), rule))) for ch in current)


def _percentages(plan: _Plan, token_cap: int | None, threshold: int) -> dict[str, int]:
    out: dict[str, int] = {}
    spent = 0
    for cid, src, past in plan:
        if len(past) > REF_CAP:
            raise HeatBoundError()
        top = 0
        for ref in past:
            before = _Work.cells
            top = max(top, similarity(src, ref, token_cap=token_cap))
            spent += _Work.cells - before
            if spent > CELL_BUDGET:
                raise HeatBoundError()
        if top >= threshold:
            out[cid] = top
    return out


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
    plan = _plan(challenges, validations, round_no, rule, refs)
    bound = pinned_bound(rule)
    key = _memo_key(rule, bound, plan)
    if similarity is _REAL_SIMILARITY:
        cached = _MEMO.get(key)
        if cached is not None:
            _MEMO.move_to_end(key)
            return dict(cached)
    out = _percentages(plan, None if bound is None else bound[0], rule.heat_threshold_pct)
    if similarity is _REAL_SIMILARITY:
        _MEMO[key] = dict(out)
        while len(_MEMO) > _MEMO_MAX:
            _MEMO.popitem(last=False)
    return out


def _memo_key(rule: LedgerRule, bound: tuple[int, int] | None, plan: _Plan) -> tuple[object, ...]:
    compared = tuple((cid, _digest(src), tuple(_digest(s) for s in past)) for cid, src, past in plan)
    return (rule.heat_threshold_pct, bound, compared)


def _digest(source: str) -> bytes:
    raw = _raw(source)
    return b"\x00" if raw is None else hashlib.sha256(raw).digest()


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


def history_refs(
    past: Iterable[MatchState],
    agents: Iterable[AgentSpec],
    k: int = 3,
    *,
    source_limit: int | None = None,
) -> dict[str, list[str]]:
    """agent name -> valid challenge sources its model (same provider and model) built in its last ``k`` matches,
    oldest first by ``created_seq``. ``source_limit`` keeps the newest sources so a new match stores a bounded
    list. Frozen into ``match_created`` so heat replays exactly."""
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
        if source_limit is not None:
            sources = sources[-source_limit:]
        if sources:
            out[spec.name] = sources
    return out


_REAL_SIMILARITY = similarity
