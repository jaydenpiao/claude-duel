"""The ``check`` challenge form (rules.challenge_form = "check"): the author writes only ``def check(s)``.

The arena owns all I/O. It appends ``WRAPPER``, which reads stdin and prints FLAG when ``check`` returns True,
so authors never touch FLAG or stdin. A solver runs after the author's definitions, so it can call ``check``
(for example to search a small space) without NameErrors. Pure functions; the engine does the running.
"""

import ast

# The flag goes out through the real ``sys.stdout`` (authors may not name ``sys``), never through ``print``,
# which author code could rebind to transform its own flag.
# R5: the arena's published 32-bit integer mixer, so "seeded" designs need no hashlib (banned in this form).
# Defined after the author's code: call mix() inside functions, not at top level.
MIX_DEF = (
    "def mix(x):\n"
    "    x = int(x) & 0xFFFFFFFF\n"
    "    x = ((x ^ (x >> 16)) * 0x45D9F3B) & 0xFFFFFFFF\n"
    "    x = ((x ^ (x >> 16)) * 0x45D9F3B) & 0xFFFFFFFF\n"
    "    return x ^ (x >> 16)\n"
)
_PRE_MIX_WRAPPER = "\n\nimport sys\nif check(sys.stdin.read().strip()) is True:\n    sys.stdout.write(FLAG + '\\n')\n"
WRAPPER = "\n\n" + MIX_DEF + _PRE_MIX_WRAPPER
_LEGACY_WRAPPERS = (
    _PRE_MIX_WRAPPER,
    "\n\nimport sys\nif check(sys.stdin.read().strip()) is True:\n    print(FLAG)\n",
)
_IO_NAMES = frozenset({"FLAG", "sys"})
_PROTECTED = frozenset({"print", "check", "mix"})  # may not be rebound (check only by its one top-level def)


# Attack-time wrapper (#100): a failed try says whether check() returned False or a non-bool. An exception
# is already reported by the sandbox runner as type and line. Never a value check() computed, so the answer
# doesn't leak. Like WRAPPER, it writes through sys.stdout, never a rebindable print.
FEEDBACK_WRAPPER = (
    "\n\n" + MIX_DEF + "\n\nimport sys\n_ok = check(sys.stdin.read().strip())\n"
    "_msg = FLAG if _ok is True else 'check returned False' if _ok is False else 'check returned a non-bool'\n"
    "sys.stdout.write(_msg + '\\n')\n"
)


def with_feedback(source: str) -> str:
    """The composed challenge with the feedback wrapper; other challenge forms pass through unchanged."""
    return source[: -len(WRAPPER)] + FEEDBACK_WRAPPER if source.endswith(WRAPPER) else source


def definitions(source: str) -> str:
    """The author's part of a composed source (idempotent: a repaired answer may echo the wrapper back)."""
    for wrapper in (WRAPPER, *_LEGACY_WRAPPERS):
        if source.endswith(wrapper):
            return source[: -len(wrapper)]
    return source


def _bound(node: ast.AST, check_def: ast.FunctionDef) -> tuple[str, ...]:
    """Names one node binds (``check_def`` itself is the one allowed binding of ``check``)."""
    if isinstance(node, ast.Name):
        return (node.id,) if isinstance(node.ctx, (ast.Store, ast.Del)) else ()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return () if node is check_def else (node.name,)
    if isinstance(node, ast.alias):
        return (node.asname or node.name,)
    if isinstance(node, ast.arg):
        return (node.arg,)
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return tuple(node.names)
    return ()


def _rebinds(tree: ast.Module, check_def: ast.FunctionDef) -> set[str]:
    """Protected names the author binds: assignments, defs, classes, imports, args, globals, decorators on check."""
    out = {name for node in ast.walk(tree) for name in _bound(node, check_def)}
    if check_def.decorator_list:
        out.add("check")
    return out & _PROTECTED


def _io_used(tree: ast.Module) -> set[str]:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return (names | {n.name for n in ast.walk(tree) if isinstance(n, ast.alias)}) & _IO_NAMES


def _problem(tree: ast.Module) -> str | None:
    """Why a parsed ``check`` source can't be composed, or None."""
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "check"]
    if len(funcs) != 1 or len(funcs[0].args.args) != 1:
        return "the source must define exactly one top-level function check(s) taking the input string"
    rebound = _rebinds(tree, funcs[0])
    if rebound:
        return f"do not rebind {', '.join(sorted(rebound))}: the arena owns print and calls your one check(s)"
    used = _io_used(tree)
    if used:
        return (
            f"do not use {', '.join(sorted(used))}: the arena reads stdin and prints FLAG when check(s) returns "
            "True; write only check(s) and its helpers"
        )
    return None


def compose(source: str) -> tuple[str, str | None]:
    """The runnable challenge for an author's ``check`` source, or the reason it can't be one."""
    own = definitions(source).rstrip()
    try:
        tree = ast.parse(own)
    except SyntaxError as exc:
        return own, f"syntax error: {exc.msg} (line {exc.lineno})"
    reason = _problem(tree)
    return (own, reason) if reason else (own + WRAPPER, None)


def with_mix(source: str) -> str:
    """The author's definitions plus the arena's ``mix`` (no I/O wrapper)."""
    return definitions(source).rstrip() + "\n\n" + MIX_DEF


def solver_program(source: str, solver: str) -> str:
    """The author's definitions (and ``mix``) followed by the solver, so the solver may call ``check``."""
    return with_mix(source) + "\n\n" + solver
