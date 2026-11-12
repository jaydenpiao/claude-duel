"""Heat token walk and host limits (design-core-logic.md 4.3). Pure: no caches, no I/O.

A source becomes the pre-order walk of its AST (node, then ``ast.iter_child_nodes`` in field order): the node
type, plus the value type for ``Constant`` and the operator class names for ``BinOp``/``UnaryOp``/``BoolOp``/
``Compare``. Names, literal values, comments and formatting are dropped. Sources over ``PARSE_BYTES`` are not
parsed. A source the parser rejects (including deep nesting) walks to nothing, the same as a syntax error.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

PARSE_BYTES = 65_536  # heat never parses more; whole-source heat admits no larger source
TOKEN_CAP = 256  # prefix width ceiling, and the legacy rule's exact-LCS ceiling
REF_CAP = 48  # references per challenge
STORE_LIMIT = 32_768  # tokens kept per source; a longer walk keeps its length and counts only

_SKIP = (ast.expr_context, ast.operator, ast.unaryop, ast.boolop, ast.cmpop)  # ops are emitted by their parent


def raw(source: str) -> bytes | None:
    """UTF-8 bytes to parse, or None over ``PARSE_BYTES`` (a long string is not encoded past that)."""
    if len(source) > PARSE_BYTES:
        return None
    data = source.encode()
    return None if len(data) > PARSE_BYTES else data


def digest(source: str) -> bytes:
    """Cache key for a source; every unparsed source shares one key."""
    data = raw(source)
    return b"\x00" if data is None else hashlib.sha256(data).digest()


@dataclass(frozen=True)
class Profile:
    """A source's walk: the first ``STORE_LIMIT`` tokens, the true length and the token counts."""

    kept: tuple[str, ...]
    length: int
    counts: dict[str, int]

    @property
    def tokens(self) -> tuple[str, ...] | None:
        """The whole walk, or None when it is longer than ``STORE_LIMIT``."""
        return self.kept if self.length == len(self.kept) else None


EMPTY = Profile((), 0, {})


def _extra(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Constant):
        return (type(node.value).__name__,)
    if isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp)):
        return (type(node.op).__name__,)
    if isinstance(node, ast.Compare):
        return tuple(type(op).__name__ for op in node.ops)
    return ()


def profile(source: str) -> Profile | None:
    """The walk of ``source``; None over ``PARSE_BYTES``; ``EMPTY`` when it does not parse."""
    if raw(source) is None:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return EMPTY
    kept: list[str] = []
    counts: dict[str, int] = {}
    length = 0
    stack: list[ast.AST] = list(reversed(tree.body))  # the Module root is in every file, so it is not a token
    while stack:
        node = stack.pop()
        if isinstance(node, _SKIP):
            continue
        for text in (type(node).__name__, *_extra(node)):
            length += 1
            counts[text] = counts.get(text, 0) + 1
            if length <= STORE_LIMIT:
                kept.append(text)
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return Profile(tuple(kept), length, counts)


def tokens(source: str, limit: int = TOKEN_CAP) -> tuple[str, ...]:
    """The first ``limit`` tokens (at most ``STORE_LIMIT``); () when the source does not parse or is too big."""
    got = profile(source)
    return () if got is None else got.kept[:limit]
