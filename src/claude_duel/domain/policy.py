"""Static challenge policy: defence in depth before anything executes."""

import ast

from claude_duel.domain.flags import FLAG_PATTERN
from claude_duel.domain.models import ChallengeSpec

ALLOWED_IMPORTS = frozenset(
    {"sys", "os", "hashlib", "base64", "json", "re", "string", "math", "itertools", "binascii", "zlib", "struct"}
)
BANNED_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "breakpoint", "globals", "vars", "getattr", "setattr", "input"}
)
BANNED_ATTRS = frozenset(
    {"system", "popen", "fork", "spawnv", "execv", "execve", "kill", "remove", "unlink", "rmdir", "putenv", "environb"}
)


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
    for node in ast.walk(tree):
        reason = _check_node(node)
        if reason:
            return reason
    return None


def _check_node(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        mods = [a.name.split(".")[0] for a in node.names]
    elif isinstance(node, ast.ImportFrom):
        mods = [(node.module or "").split(".")[0]]
    else:
        mods = []
    for m in mods:
        if m not in ALLOWED_IMPORTS:
            return f"import not allowed: {m}"
    if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
        return f"banned name: {node.id}"
    if isinstance(node, ast.Attribute) and (node.attr in BANNED_ATTRS or node.attr.startswith("__")):
        return f"banned attribute: {node.attr}"
    return None
