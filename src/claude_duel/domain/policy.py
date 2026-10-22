"""Static challenge policy: an AST allowlist checked before anything executes.

Challenge code gets the flag as the global ``FLAG`` and its input on ``sys.stdin``.
It may only import pure modules, may touch ``sys`` only through ``stdin``/``stdout``,
and may not reference any name or attribute starting with ``_``.
"""

import ast
import re

from claude_duel.domain.flags import FLAG_PATTERN
from claude_duel.domain.models import ChallengeSpec

PURE_MODULES = frozenset(
    {"hashlib", "base64", "json", "re", "string", "math", "itertools", "binascii", "zlib", "struct"}
)
RESTRICTED_MODULES: dict[str, frozenset[str]] = {"sys": frozenset({"stdin", "stdout"})}
ALLOWED_IMPORTS = PURE_MODULES | frozenset(RESTRICTED_MODULES)
BANNED_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "breakpoint", "globals", "locals", "vars", "dir",
        "getattr", "setattr", "delattr", "hasattr", "input", "help", "memoryview",
        "exit", "quit", "type", "object", "super", "classmethod", "staticmethod",
    }
)  # fmt: skip
_FORMAT_ATTR = re.compile(r"\{[^{}]*[.\[]")


def check(spec: ChallengeSpec, max_source_bytes: int) -> str | None:
    """Return a rejection reason, or None if the challenge passes."""
    if len(spec.source.encode()) > max_source_bytes:
        return f"source exceeds {max_source_bytes} bytes"
    for field in (spec.source, spec.description, spec.reference_payload):
        if FLAG_PATTERN.search(field):
            return "flag-shaped literal present"
    try:
        tree = ast.parse(spec.source)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg}"
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        reason = _check_node(node, parents.get(node))
        if reason:
            return reason
    return None


def _check_node(node: ast.AST, parent: ast.AST | None) -> str | None:
    if isinstance(node, ast.Import):
        return _check_import(node)
    if isinstance(node, ast.ImportFrom):
        return _check_import_from(node)
    if isinstance(node, ast.Name):
        return _check_name(node, parent)
    if isinstance(node, ast.Attribute):
        return _check_attribute(node)
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and _FORMAT_ATTR.search(node.value):
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
    allowed = RESTRICTED_MODULES.get(module)
    for alias in node.names:
        if alias.name == "*":
            return "star import not allowed"
        if alias.name.startswith("_") or (allowed is not None and alias.name not in allowed):
            return f"import not allowed: {module}.{alias.name}"
    return None


def _check_name(node: ast.Name, parent: ast.AST | None) -> str | None:
    if node.id == "FLAG" and not isinstance(node.ctx, ast.Load):
        return "FLAG is injected by the arena; never assign or define it"
    if node.id.startswith("_") or node.id in BANNED_NAMES:
        return f"banned name: {node.id}"
    if node.id in RESTRICTED_MODULES and not (isinstance(parent, ast.Attribute) and parent.value is node):
        return f"{node.id} may only be used as {node.id}.<attr>"
    return None


def _check_attribute(node: ast.Attribute) -> str | None:
    if node.attr.startswith("_"):
        return f"banned attribute: {node.attr}"
    module = node.value.id if isinstance(node.value, ast.Name) else None
    if module in RESTRICTED_MODULES and node.attr not in RESTRICTED_MODULES[module]:
        return f"banned attribute: {module}.{node.attr}"
    return None
