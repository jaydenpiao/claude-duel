"""Arena admission then whole-source heat. A copied body stays hot; a short template still dilutes."""

from __future__ import annotations

import ast

from claude_duel.adapters.providers.fake import TEMPLATES
from claude_duel.app.engine import MatchEngine
from claude_duel.domain.heat import TOKEN_CAP, similarity
from claude_duel.domain.models import (
    AgentSpec,
    Attack,
    Challenge,
    ChallengeSpec,
    MatchState,
    SandboxLimits,
    Validation,
)
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import round_heat, score_round

_SUM = next(t for t in TEMPLATES if t.key == "sum")
_CELL_BUDGET = 8_000_000


def _body(n: int) -> str:
    """The shipped sum challenge plus straight-line statements. No loops, no unbound names."""
    lines = [_SUM.source]
    for i in range(n):
        lines.append(f"v{i} = {i % 5}\n")
        if i % 4 == 0:
            lines.append(f"w{i} = v{i} + 1\n")
        if i % 9 == 0:
            lines.append(f"c{i} = v{i} < 4\n")
        if i % 11 == 0:
            lines.append(f"p{i} = [v{i}]\n")
        if i % 13 == 0:
            lines.append(f"t{i} = (v{i},)\n")
    return "".join(lines)


def _full_tokens(source: str, limit: int = 6000) -> tuple[str, ...]:
    """Test-only walk with a hard stop. Production heat never uses this."""
    tree = ast.parse(source)
    out: list[str] = []
    stack = list(reversed(tree.body))
    skip = (ast.expr_context, ast.operator, ast.unaryop, ast.boolop, ast.cmpop)
    while stack and len(out) < limit:
        node = stack.pop()
        if isinstance(node, skip):
            continue
        out.append(type(node).__name__)
        if isinstance(node, ast.Constant):
            out.append(type(node.value).__name__)
        elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp)):
            out.append(type(node.op).__name__)
        elif isinstance(node, ast.Compare):
            out.extend(type(op).__name__ for op in node.ops)
        if len(out) >= limit:
            break
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return tuple(out)


def _lcs(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Bounded probe only. Not the production walker, and it refuses an oversized pair."""
    cells = len(a) * len(b)
    if cells > _CELL_BUDGET:
        raise AssertionError("fixture exceeds the probe cell budget")
    width = len(b) + 1
    prev = [0] * width
    for token in a:
        cur = [0] * width
        for j, other in enumerate(b, start=1):
            diag = prev[j - 1] + 1
            cur[j] = diag if token == other else (prev[j] if prev[j] > cur[j - 1] else cur[j - 1])
        prev = cur
    return prev[-1]


def _sim(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    if not a or not b:
        return 0
    return 100 * _lcs(a, b) // max(len(a), len(b))


def _spec(source: str, payload: str | None = None, title: str = "Arithmetic gate") -> ChallengeSpec:
    return ChallengeSpec(
        title=title,
        description="Find the input that reveals the flag.",
        source=source,
        reference_payload=_SUM.payload if payload is None else payload,
        difficulty=2,
    )


def _admit(
    engine: MatchEngine, source: str, round_no: int, payload: str | None = None, title: str = "Arithmetic gate"
) -> str | None:
    holder = MatchState(
        match_id="admit",
        secret="ab" * 32,
        agents=(AgentSpec(name="alice"), AgentSpec(name="bob")),
        rules=preset("ledger"),
        limits=SandboxLimits(),
    )
    return engine._problem(holder, "c", round_no, _spec(source, payload, title))


def test_preamble_is_admitted_and_the_prefix_misses_the_copy(make_engine) -> None:  # type: ignore[no-untyped-def]
    """Measured gap, not a proposed score: full-source LCS stays hot and the 256-token prefix does not."""
    engine = make_engine()
    body = _body(220)
    padded = "pass\n" * TOKEN_CAP + body
    assert _admit(engine, body, 2) is None
    assert _admit(engine, padded, 3) is None
    assert len(padded.encode()) <= preset("ledger").max_source_bytes(3)
    full_body, full_pad = _full_tokens(body), _full_tokens(padded)
    assert TOKEN_CAP < len(full_body) < 6000
    assert len(full_pad) < 6000
    assert similarity(padded, body, token_cap=TOKEN_CAP) < 70
    assert _sim(full_pad, full_body) >= 85


def test_admitted_preamble_still_pays_heat(make_engine) -> None:  # type: ignore[no-untyped-def]
    """A copied body after structural padding keeps the heat line. A short template does not: full LCS dilutes too."""
    engine = make_engine()
    body = _body(220)
    padded = "pass\n" * TOKEN_CAP + body
    short = _SUM.source
    short_pad = "pass\n" * TOKEN_CAP + short
    assert _admit(engine, body, 2) is None
    assert _admit(engine, padded, 3) is None
    assert _admit(engine, short_pad, 2) is None
    rules = preset("ledger")
    original = Challenge(id="r2a", round=2, author="a", spec=_spec(body))
    copy = Challenge(id="r3a", round=3, author="a", spec=_spec(body))
    hidden = Challenge(id="r3a", round=3, author="a", spec=_spec(padded))
    vals = {c.id: Validation(challenge_id=c.id, valid=True, reason="ok") for c in (original, copy)}
    held = {
        "r3a": Attack(
            challenge_id="r3a", attacker="b", solved=False, attempts=1, tokens_used=0, reason="held", stake=4000
        )
    }
    honest = score_round(3, ["a", "b"], [original, copy], vals, held, rules)
    cheated = score_round(3, ["a", "b"], [original, hidden], {**vals, hidden.id: vals["r3a"]}, held, rules)
    hot = round_heat([original, hidden], {**vals, hidden.id: vals["r3a"]}, held, 3, rules)
    assert round_heat([original, copy], vals, held, 3, rules)["r3a"] > 0
    assert hot["r3a"] > 0
    assert cheated["a"] == honest["a"]
    # Length dilution is the old whole-source result: a 73-token template after 256 passes stays under 70.
    short_ch = Challenge(id="r1a", round=1, author="a", spec=_spec(short))
    diluted = Challenge(id="r2a", round=2, author="a", spec=_spec(short_pad))
    short_vals = {c.id: Validation(challenge_id=c.id, valid=True, reason="ok") for c in (short_ch, diluted)}
    short_held = {
        "r2a": Attack(
            challenge_id="r2a", attacker="b", solved=False, attempts=1, tokens_used=0, reason="held", stake=4000
        )
    }
    assert round_heat([short_ch, diluted], short_vals, short_held, 2, rules) == {}


# Structurally different uncalled helpers (not a renamed copy). The heat walk drops names.
_HELPERS = (
    "def keep(a):\n    return a\n",
    "def keep(a):\n    return a + 1\n",
    "def keep(a):\n    return a - 1\n",
    "def keep(a):\n    return [a]\n",
    "def keep(a):\n    return (a, a)\n",
    "def keep(a):\n    return a == a\n",
)


def _grow(shapes: tuple[str, ...], n_tokens: int) -> str:
    out = ""
    step = 0
    while len(_full_tokens(out)) < n_tokens and step < n_tokens * 4:
        out += shapes[step % len(shapes)]
        step += 1
    return out


def test_structural_helper_preamble_heats_and_a_different_template_does_not(make_engine) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine()
    body = _body(220)
    hidden_src = _grow(_HELPERS, TOKEN_CAP) + body
    maze = next(item for item in TEMPLATES if item.key == "password")
    assert _admit(engine, hidden_src, 3) is None
    assert _admit(engine, maze.source, 1, maze.payload, maze.title) is None
    rules = preset("ledger")
    original = Challenge(id="r2a", round=2, author="a", spec=_spec(body))
    hidden = Challenge(id="r3a", round=3, author="a", spec=_spec(hidden_src))
    other = Challenge(id="r2b", round=2, author="b", spec=_spec(maze.source, maze.payload, maze.title))
    vals = {c.id: Validation(challenge_id=c.id, valid=True, reason="ok") for c in (original, hidden, other)}
    held = {
        "r3a": Attack(
            challenge_id="r3a", attacker="b", solved=False, attempts=1, tokens_used=0, reason="held", stake=4000
        )
    }
    assert round_heat([original, hidden], vals, held, 3, rules)["r3a"] > 0
    assert round_heat([other, hidden], vals, held, 3, rules) == {}
