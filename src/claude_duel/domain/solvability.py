"""Solvability band for the check form (design rec R1): pure helpers; the engine does the running.

Upper bound: the arena counts how many times the author's solver calls ``check`` (or any helper of the source). A solver that needs more
calls than the rules allow is only reachable by brute force, which a checked-mode attacker (no code) can't do.

Lower bound: the arena runs ``check`` on seeded random inputs shaped like the reference payload. If too many
pass, guessing wins, so the challenge goes back to repair.

Both counts are written by arena code after a nonce the author never sees, so the solver can't forge them.
"""

import ast
import hashlib
import math
import string

from claude_duel.domain.contract import definitions, with_mix

DENSITY_MAX_LENGTH = 64  # longer payloads have a huge space; sampling them says nothing useful
_ALPHABETS = (
    string.digits,
    string.ascii_lowercase,
    string.ascii_uppercase,
    string.ascii_letters,
    string.ascii_lowercase + string.digits,
    string.ascii_letters + string.digits,
)
_PRINTABLE = "".join(c for c in string.printable if not c.isspace())


def nonce(secret: str, cid: str, purpose: str) -> str:
    return "N" + hashlib.sha256(f"solvability:{secret}:{cid}:{purpose}".encode()).hexdigest()[:24]


def counted(source: str, solver: str, tag: str) -> str:
    """The solver program with every call into the author's top-level functions counted (``check`` and helpers
    alike; calls made inside a counted call are free). The count is written last, after ``tag``."""
    defs = with_mix(source).rstrip()
    try:
        tree = ast.parse(definitions(source))
        funcs = sorted(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    except SyntaxError:
        funcs = ["check"]
    hook = (
        "\n\n_arena_calls = [0]\n_arena_depth = [0]\n"
        "def _arena_count(f):\n"
        "    def counted(*a, **k):\n"
        "        if _arena_depth[0]:\n            return f(*a, **k)\n"
        "        _arena_calls[0] += 1\n        _arena_depth[0] = 1\n"
        "        try:\n            return f(*a, **k)\n        finally:\n            _arena_depth[0] = 0\n"
        "    return counted\n" + "".join(f"{name} = _arena_count({name})\n" for name in funcs)
    )
    tail = f"\n\nimport sys\nsys.stdout.write('\\n{tag}:' + str(_arena_calls[0]) + '\\n')\n"
    return defs + hook + "\n\n" + solver + tail


def _functions(tree: ast.Module) -> set[str]:
    """Top-level functions and classes: each is a way into the author's code."""
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _escapes(tree: ast.AST, names: set[str]) -> list[str]:
    """Names from ``names`` used other than as a direct call target (``g = check``, ``[verify]``, ``return f``)."""
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return sorted(
        {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load) and id(n) not in calls
        }
    )


def _returns_function(tree: ast.Module) -> bool:
    """A lambda outside a function body, or a function that returns or yields a lambda or a nested def."""
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n, ast.Lambda) for n in ast.walk(node)):
                return True
            continue
        nested = {n.name for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))} - {
            node.name
        }
        for n in ast.walk(node):
            value = n.value if isinstance(n, (ast.Return, ast.Yield)) else None
            if isinstance(value, ast.Lambda) or (isinstance(value, ast.Name) and value.id in nested):
                return True
    return False


def uncounted_path(source: str, solver: str) -> str | None:
    """Why the solver could run the author's code without the arena counting it, or None.

    ``counted`` counts every call into a top-level function of the source. So the source may use its functions
    (``check`` included) only by calling them, may not hand out a lambda or nested def, and the solver may only
    call them, never alias them (``g = check``) or touch a class.
    """
    try:
        src, sol = ast.parse(source), ast.parse(solver)
    except SyntaxError:
        return None  # the policy check reports syntax errors
    funcs = _functions(src)
    aliased = _escapes(src, funcs)
    if aliased:
        return f"source aliases {', '.join(aliased)}; only call your functions"
    if _returns_function(src):
        return "source hands out a lambda or nested function; return values, not functions"
    classes = sorted(n.name for n in src.body if isinstance(n, ast.ClassDef))
    used = sorted(set(_escapes(sol, funcs)) | {n.id for n in ast.walk(sol) if isinstance(n, ast.Name)} & set(classes))
    if used:
        return f"solver may only call check(), mix() and your functions, not use {', '.join(used)}"
    return None


def split_count(stdout: str, tag: str) -> tuple[str, int | None]:
    """(stdout without the arena's count line, the count or None when the solver never reached the end)."""
    head, sep, rest = stdout.rpartition(f"\n{tag}:")
    if not sep:
        return stdout, None
    line = rest.strip()
    return (head, int(line)) if line.isdigit() else (stdout, None)


def alphabet(payload: str) -> str:
    """The smallest common character class that holds the reference payload."""
    for chars in _ALPHABETS:
        if payload and all(c in chars for c in payload):
            return chars
    return _PRINTABLE


def log2_space(payload: str) -> float:
    return round(len(payload) * math.log2(len(alphabet(payload))), 2)


def samples(seed: str, cid: str, payload: str, n: int) -> list[str]:
    """``n`` inputs of the payload's length and alphabet, drawn from ``sha256(seed:cid:i)``: same on replay."""
    chars, out = alphabet(payload), []
    for i in range(n):
        digest, block = b"", 0
        while len(digest) < len(payload):
            digest += hashlib.sha256(f"{seed}:{cid}:{i}:{block}".encode()).digest()
            block += 1
        out.append("".join(chars[b % len(chars)] for b in digest[: len(payload)]))
    return out


def density_program(source: str, inputs: list[str], tag: str) -> str:
    """Runs ``check`` on every input; a raise or a non-True result counts as a miss."""
    return (
        with_mix(source).rstrip()
        + f"\n\n_arena_inputs = {inputs!r}\n_arena_hits = 0\n"
        + "for _arena_s in _arena_inputs:\n"
        + "    try:\n        _arena_hits += check(_arena_s) is True\n"
        + "    except Exception:\n        pass\n"
        + f"import sys\nsys.stdout.write('\\n{tag}:' + str(_arena_hits) + '\\n')\n"
    )


def ppm(hits: int, n: int) -> int:
    return round(hits * 1_000_000 / n) if n else 0
