"""Solvability band for the check form (design rec R1): pure helpers; the engine does the running.

Upper bound: the arena counts how many times the author's solver calls ``check``. A solver that needs more
calls than the rules allow is only reachable by brute force, which a checked-mode attacker (no code) can't do.

Lower bound: the arena runs ``check`` on seeded random inputs shaped like the reference payload. If too many
pass, guessing wins, so the challenge goes back to repair.

Both counts are written by arena code after a nonce the author never sees, so the solver can't forge them.
"""

import ast
import hashlib
import math
import string

from claude_duel.domain.contract import with_mix

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
    """The solver program with ``check`` counted; the count is written last, after ``tag``."""
    defs = with_mix(source).rstrip()
    hook = (
        "\n\n_arena_calls = [0]\n_arena_check = check\n"
        "def check(s):\n    _arena_calls[0] += 1\n    return _arena_check(s)\n"
    )
    tail = f"\n\nimport sys\nsys.stdout.write('\\n{tag}:' + str(_arena_calls[0]) + '\\n')\n"
    return defs + hook + "\n\n" + solver + tail


_SOLVER_MAY_LOAD = frozenset({"check", "mix"})


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        else:
            names.update(n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
    return names


def uncounted_path(source: str, solver: str) -> str | None:
    """Why the solver could reach the author's code without a counted ``check()`` call, or None.

    The solver may load only ``check`` and ``mix`` of the source's top-level names, and the source may use
    ``check`` only by calling it, so no alias (``g = check``) or helper is an uncounted path.
    """
    try:
        src, sol = ast.parse(source), ast.parse(solver)
    except SyntaxError:
        return None  # the policy check reports syntax errors
    calls = {id(n.func) for n in ast.walk(src) if isinstance(n, ast.Call)}
    if any(
        isinstance(n, ast.Name) and n.id == "check" and isinstance(n.ctx, ast.Load) and id(n) not in calls
        for n in ast.walk(src)
    ):
        return "source aliases check(); call it, don't pass it around"
    loads = {n.id for n in ast.walk(sol) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    imported = {
        (a.asname or a.name).split(".")[0]
        for n in ast.walk(sol)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    foreign = sorted((loads & _top_level_names(src)) - _SOLVER_MAY_LOAD - imported)
    if foreign:
        return f"solver may call only check() and mix() of your code, not {', '.join(foreign)}"
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
