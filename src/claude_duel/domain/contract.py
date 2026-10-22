"""The ``check`` challenge form (rules.challenge_form = "check"): the author writes only ``def check(s)``.

The arena owns all I/O. It appends ``WRAPPER``, which reads stdin and prints FLAG when ``check`` returns True,
so authors never touch FLAG or stdin. A solver runs after the author's definitions, so it can call ``check``
(for example to search a small space) without NameErrors. Pure functions; the engine does the running.
"""

import ast

# R3-3: the flag goes out through the real ``sys.stdout`` (authors may not name ``sys``), never through ``print``,
# which author code could rebind to transform its own flag.
WRAPPER = "\n\nimport sys\nif check(sys.stdin.read().strip()) is True:\n    sys.stdout.write(FLAG + '\\n')\n"
_LEGACY_WRAPPERS = ("\n\nimport sys\nif check(sys.stdin.read().strip()) is True:\n    print(FLAG)\n",)
_IO_NAMES = frozenset({"FLAG", "sys"})
_PROTECTED = frozenset({"print", "check"})  # may not be rebound (check only by its one top-level def)


# Attack-time wrapper (#100): a failed try says whether check() returned False or a non-bool. An exception
# is already reported by the sandbox runner as type and line. Never a value check() computed, so the answer
# doesn't leak.
FEEDBACK_WRAPPER = (
    "\n\nimport sys\n_ok = check(sys.stdin.read().strip())\n"
    "print(FLAG if _ok is True else 'check returned False' if _ok is False else 'check returned a non-bool')\n"
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


def _rebinds(tree: ast.Module, check_def: ast.FunctionDef) -> set[str]:
    """Protected names the author binds: assignments, defs, classes, imports, args, globals, decorators on check."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not check_def:
            out.add(node.name)
        elif isinstance(node, ast.alias):
            out.add(node.asname or node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
    if check_def.decorator_list:
        out.add("check")
    return out & _PROTECTED


def compose(source: str) -> tuple[str, str | None]:
    """The runnable challenge for an author's ``check`` source, or the reason it can't be one."""
    own = definitions(source).rstrip()
    try:
        tree = ast.parse(own)
    except SyntaxError as exc:
        return own, f"syntax error: {exc.msg} (line {exc.lineno})"
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "check"]
    if len(funcs) != 1 or len(funcs[0].args.args) != 1:
        return own, "the source must define exactly one top-level function check(s) taking the input string"
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.name for n in ast.walk(tree) if isinstance(n, ast.alias)
    }
    rebound = _rebinds(tree, funcs[0])
    if rebound:
        return own, f"do not rebind {', '.join(sorted(rebound))}: the arena owns print and calls your one check(s)"
    used = names & _IO_NAMES
    if used:
        return own, (
            f"do not use {', '.join(sorted(used))}: the arena reads stdin and prints FLAG when check(s) returns "
            "True; write only check(s) and its helpers"
        )
    return own + WRAPPER, None


def solver_program(source: str, solver: str) -> str:
    """The author's definitions followed by the solver, so the solver may call ``check``."""
    return definitions(source).rstrip() + "\n\n" + solver
