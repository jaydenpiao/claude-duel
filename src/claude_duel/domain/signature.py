"""Structural fingerprints of a challenge's source (design-variety-solvability R2/R5). Pure, static, no model.

``signatures`` finds the lock-family shapes R2 binds against: a state machine (``state = T[(state, c)]`` over
the input), a digit lock (``s.isdigit()`` plus ``int()`` of single characters) and
bitwise char-code mixing (at least three xor/shift/mask operations next to ``ord``). ``tokens`` is the pre-order
AST node sequence (identifiers and constants erased), so renaming variables does not change it; ``fingerprint``
is a short digest of it.
"""

import ast
import hashlib

SIGNATURES = {  # signature -> the lock family it marks
    "state-machine": "state machine (transitions over input symbols)",
    "digit-lock": "digit constraints (sums, products, order, modulo)",
    "bitwise": "bit manipulation (xor, shifts, masks on char codes)",
}
_BIT_OPS = (ast.BitXor, ast.LShift, ast.RShift, ast.BitAnd)


def _parse(source: str) -> ast.AST | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _loop_vars(tree: ast.AST) -> set[str]:
    """Names bound by ``for x in ...`` / ``for i, x in enumerate(...)`` loops and comprehensions."""
    out: set[str] = set()
    for node in ast.walk(tree):
        target = node.target if isinstance(node, (ast.For, ast.comprehension)) else None
        if target is not None:
            out |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
    return out


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _lookup_key(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Subscript):
        return node.slice
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        return node.args[0]
    return None


def _is_fsm(tree: ast.AST, loop: set[str]) -> bool:
    """``state = T[(state, c)]`` (or ``T.get((state, c), ...)``) with ``c`` a loop variable: the state feeds back."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        key = _lookup_key(node.value)
        if isinstance(key, ast.Tuple) and len(key.elts) >= 2:
            names = _names(key)
            if node.targets[0].id in names and names & loop:
                return True
    return False


def _is_digit_lock(tree: ast.AST, loop: set[str]) -> bool:
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    isdigit = any(isinstance(c.func, ast.Attribute) and c.func.attr == "isdigit" for c in calls)
    char_ints = [
        c
        for c in calls
        if isinstance(c.func, ast.Name)
        and c.func.id == "int"
        and len(c.args) == 1
        and (
            (isinstance(c.args[0], ast.Subscript) and not isinstance(c.args[0].slice, ast.Slice))
            or (isinstance(c.args[0], ast.Name) and c.args[0].id in loop)
        )
    ]
    return isdigit and bool(char_ints)


def _is_bitwise(tree: ast.AST) -> bool:
    ops = sum(isinstance(n, ast.BinOp) and isinstance(n.op, _BIT_OPS) for n in ast.walk(tree))
    uses_ord = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ord" for n in ast.walk(tree)
    )
    return uses_ord and ops >= 3


def signatures(source: str) -> list[str]:
    """Lock-family signatures found in ``source`` (sorted; empty when none or unparsable)."""
    tree = _parse(source)
    if tree is None:
        return []
    loop = _loop_vars(tree)
    found = {
        "state-machine": _is_fsm(tree, loop),
        "digit-lock": _is_digit_lock(tree, loop),
        "bitwise": _is_bitwise(tree),
    }
    return sorted(k for k, hit in found.items() if hit)


def tokens(source: str) -> tuple[str, ...]:
    """Pre-order AST node types, operators kept, identifiers and constant values erased."""
    tree = _parse(source)
    if tree is None:
        return ()
    out: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.Load, ast.Store, ast.Del)):
            return
        out.append(type(node).__name__)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return tuple(out)


def fingerprint(source: str) -> str:
    """6 hex of the token sequence: equal for sources that differ only in names and constants."""
    return hashlib.sha256(" ".join(tokens(source)).encode()).hexdigest()[:6]
