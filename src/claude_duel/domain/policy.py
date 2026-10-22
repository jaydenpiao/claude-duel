"""Static challenge policy: an AST allowlist checked before anything executes.

Challenge code gets the flag as the global ``FLAG`` and its input on ``sys.stdin``.
It may only import pure modules, may touch ``sys`` only through ``stdin``/``stdout``/``exit``,
and may not reference any name or attribute starting with ``_``.
"""

import ast
import base64
import binascii
import importlib
import re
import types
from collections.abc import Callable
from typing import Any

from claude_duel.domain.flags import FLAG_PATTERN
from claude_duel.domain.models import ChallengeSpec

PURE_MODULES = frozenset(
    {"hashlib", "base64", "json", "re", "string", "math", "itertools", "binascii", "zlib", "struct"}
)
RESTRICTED_MODULES: dict[str, frozenset[str]] = {"sys": frozenset({"stdin", "stdout", "exit"})}
ALLOWED_IMPORTS = PURE_MODULES | frozenset(RESTRICTED_MODULES)
BANNED_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "breakpoint", "globals", "locals", "vars", "dir",
        "getattr", "setattr", "delattr", "hasattr", "input", "help", "memoryview",
        "exit", "quit", "type", "object", "super", "classmethod", "staticmethod",
    }
)  # fmt: skip
# Builtins the runner gives challenge code; everything else (incl. the real __import__) is absent at runtime.
SAFE_BUILTINS = frozenset(
    {
        "abs", "all", "any", "bin", "bool", "bytearray", "bytes", "chr", "dict", "divmod", "enumerate",
        "filter", "float", "frozenset", "hash", "hex", "int", "isinstance", "iter", "len", "list", "map",
        "max", "min", "next", "oct", "ord", "pow", "print", "range", "repr", "reversed", "round", "set",
        "slice", "sorted", "str", "sum", "tuple", "zip",
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
        "UnicodeDecodeError", "UnicodeEncodeError", "StopIteration", "ArithmeticError", "LookupError",
    }
)  # fmt: skip
# CPython introspection attributes without a leading underscore (generator/coroutine/frame/code/traceback).
_INTROSPECTION_ATTR = re.compile(r"^(gi|cr|ag|f|co|tb)_|^(mro|modules|builtins)$")
# str.format/format_map/string.Formatter do getattr on fields built at runtime (red-team F1).
FORMAT_ATTRS = frozenset({"format", "format_map", "vformat", "Formatter"})
_FORMAT_ATTR = re.compile(r"\{[^{}]*[.\[]")
MIN_PAYLOAD_CHARS = 4
_FLAG_HINT = "FLAG is already a global; use it directly"
_NAME_HINTS = {
    "globals": _FLAG_HINT, "locals": _FLAG_HINT, "vars": _FLAG_HINT,
    "input": "read sys.stdin.read() instead", "exit": "use sys.exit()", "quit": "use sys.exit()",

}  # fmt: skip
# A tiny grid walk: the answer is a path, never a literal (the build prompt's example; also a fake-provider template).
MAZE_SOURCE = (
    "import sys\n"
    'grid = ["S.#", ".##", "..G"]\n'
    "r = c = 0\n"
    "for m in sys.stdin.read().strip():\n"
    '    dr, dc = {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}.get(m, (9, 9))\n'
    "    r, c = r + dr, c + dc\n"
    '    if not (0 <= r < 3 and 0 <= c < 3) or grid[r][c] == "#":\n'
    "        break\n"
    "else:\n"
    '    if grid[r][c] == "G":\n'
    "        print(FLAG)\n"
)


def check(spec: ChallengeSpec, max_source_bytes: int) -> str | None:
    """Return a rejection reason, or None if the challenge passes."""
    for field in (spec.source, spec.description, spec.reference_payload, spec.solver):
        if FLAG_PATTERN.search(field):
            return "flag-shaped literal present"
    return check_source(spec.source, max_source_bytes) or answer_exposed(ast.parse(spec.source), spec.reference_payload)


def check_source(source: str, max_source_bytes: int) -> str | None:
    """The static allowlist for any arena program (a challenge or its solver)."""
    if len(source.encode()) > max_source_bytes:
        return f"source exceeds {max_source_bytes} bytes"
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg} (line {exc.lineno})"
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        reason = _check_node(node, parents.get(node))
        if reason:
            return reason
    return None


def answer_exposed(tree: ast.AST, payload: str) -> str | None:
    """Reject sources that carry the accepting input verbatim or in an obvious encoding.

    Covers the payload, reversed, hex, base64 (and the payload's own hex/base64 decoding), inside any string or
    bytes literal or their concatenation, and int sequences that equal its char codes up to a constant offset or
    XOR. The attacker reads the source, so any of these is a free capture.
    """
    answer = payload.strip()
    if len(answer) < MIN_PAYLOAD_CHARS:
        return f"reference_payload must be at least {MIN_PAYLOAD_CHARS} characters"
    return _in_literals(tree, answer) or _in_char_codes(tree, answer)


def _in_literals(tree: ast.AST, answer: str) -> str | None:
    texts = [t for n in ast.walk(tree) if isinstance(n, ast.Constant) and (t := _text(n.value)) is not None]
    haystack = "\n".join([*texts, "".join(texts)]).lower()
    if any(form.lower() in haystack for form in _encodings(answer)):
        return "the accepting input (or an obvious encoding of it) appears in the source; derive it with logic"
    return None


def _in_char_codes(tree: ast.AST, answer: str) -> str | None:
    codes = [ord(c) for c in answer]
    if any(_shifted(seq, codes) or _shifted(seq, codes[::-1]) for seq in _int_sequences(tree)):
        return "the accepting input appears in the source as shifted or XORed char codes; derive it with logic"
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return value.decode("latin-1") if isinstance(value, bytes) else None


def _encodings(answer: str) -> set[str]:
    raw = answer.encode()
    forms = {answer, answer[::-1], raw.hex(), base64.b64encode(raw).decode().rstrip("=")}
    for decode in (base64.b64decode, bytes.fromhex):
        try:
            decoded = decode(answer).decode()
        except (ValueError, binascii.Error):
            continue
        if len(decoded) >= MIN_PAYLOAD_CHARS and decoded.isprintable():
            forms.add(decoded)
    return forms


def _int_sequences(tree: ast.AST) -> list[list[int]]:
    seqs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [e.value for e in node.elts if isinstance(e, ast.Constant) and type(e.value) is int]
            if len(values) == len(node.elts) >= MIN_PAYLOAD_CHARS:
                seqs.append(values)
    return seqs


def _shifted(seq: list[int], codes: list[int]) -> bool:
    """seq equals codes up to a constant offset or a constant XOR."""
    if len(seq) != len(codes):
        return False
    return (
        len({a - b for a, b in zip(seq, codes, strict=True)}) == 1
        or len({a ^ b for a, b in zip(seq, codes, strict=True)}) == 1
    )


def _check_node(node: ast.AST, parent: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return _check_name(node, parent)
    check = _CHECKS.get(type(node))
    return check(node) if check is not None else None


def _check_constant(node: ast.AST) -> str | None:
    value = node.value if isinstance(node, ast.Constant) else None
    if not isinstance(value, (str, bytes)):
        return None
    text = value if isinstance(value, str) else value.decode("latin-1")
    if "__" in text:
        return "string with a dunder ('__') not allowed"
    if _FORMAT_ATTR.search(text):
        return "format field with attribute or index access"
    return None


def _check_import(node: ast.Import) -> str | None:
    for alias in node.names:
        if alias.name not in ALLOWED_IMPORTS:
            return f"import not allowed: {alias.name}"
        if alias.asname and alias.name in RESTRICTED_MODULES:
            return f"aliasing not allowed: {alias.name}"
    return None


def _check_import_from(node: ast.ImportFrom) -> str | None:
    module = node.module or ""
    if node.level or module not in ALLOWED_IMPORTS:
        return f"import not allowed: {'.' * node.level}{module}"
    return next((r for alias in node.names if (r := _check_from_name(module, alias.name))), None)


def _check_from_name(module: str, name: str) -> str | None:
    allowed = RESTRICTED_MODULES.get(module)
    if name == "*":
        return "star import not allowed"
    if name.startswith("_") or name in FORMAT_ATTRS or (allowed is not None and name not in allowed):
        return f"import not allowed: {module}.{name}"
    if allowed is None and _is_submodule(module, name):
        return f"submodule access not allowed: {module}.{name}"
    return None


def _check_name(node: ast.Name, parent: ast.AST | None) -> str | None:
    if node.id == "FLAG" and not isinstance(node.ctx, ast.Load):
        return "FLAG is injected by the arena; never assign or define it (delete that line and just print(FLAG))"
    if (node.id.startswith("_") and node.id != "_") or node.id in BANNED_NAMES:
        return f"banned name: {node.id} ({_NAME_HINTS.get(node.id, 'rename or remove it')})"
    if node.id in ALLOWED_IMPORTS and not (isinstance(parent, ast.Attribute) and parent.value is node):
        return f"{node.id} may only be used as {node.id}.<attr>"
    return None


def _check_attribute(node: ast.Attribute) -> str | None:
    if node.attr.startswith("_") or _INTROSPECTION_ATTR.search(node.attr):
        return f"banned attribute: {node.attr}"
    if node.attr in FORMAT_ATTRS:
        return f"banned attribute: {node.attr} (use an f-string or %)"
    module = node.value.id if isinstance(node.value, ast.Name) else None
    if module in RESTRICTED_MODULES and node.attr not in RESTRICTED_MODULES[module]:
        return f"banned attribute: {module}.{node.attr} (only {', '.join(sorted(RESTRICTED_MODULES[module]))})"
    if module in PURE_MODULES and _is_submodule(module, node.attr):
        return f"submodule access not allowed: {module}.{node.attr}"
    return None


def _is_submodule(module: str, attr: str) -> bool:
    """True if ``module.attr`` is itself a module (e.g. ``re.enum``, ``json.codecs``): the way out of the allowlist."""
    return isinstance(getattr(importlib.import_module(module), attr, None), types.ModuleType)


def _check_class(node: ast.ClassDef) -> str | None:
    return f"class statements not allowed: {node.name} (use functions and dicts)"


_CHECKS: dict[type[ast.AST], Callable[[Any], str | None]] = {
    ast.ClassDef: _check_class,
    ast.Import: _check_import,
    ast.ImportFrom: _check_import_from,
    ast.Attribute: _check_attribute,
    ast.Constant: _check_constant,
}


_REASON_CODES = (
    ("import not allowed", "import"),
    ("banned attribute", "attribute"),
    ("submodule access", "submodule"),
    ("string with a dunder", "dunder-string"),
    ("banned name", "banned-name"),
    ("flag-shaped literal", "flag-literal"),
    ("FLAG is injected", "flag-assign"),
    ("the accepting input", "answer-exposed"),
    ("syntax error", "syntax"),
    ("class statements", "class"),
)


def reason_code(reason: str) -> str:
    """Machine-readable category of a rejection reason (``solver:`` prefix ignored); "other" if unknown."""
    text = reason.removeprefix("solver: ")
    return next((code for prefix, code in _REASON_CODES if text.startswith(prefix)), "other")
