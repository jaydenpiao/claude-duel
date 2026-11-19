"""Solvability band for the check form (design rec R1): pure helpers; the engine does the running.

Upper bound: the arena counts how many times the author's solver calls ``check``. A solver that needs more
calls than the rules allow is only reachable by brute force, which a checked-mode attacker (no code) can't do.

Lower bound: the arena runs ``check`` on seeded random inputs shaped like the reference payload. If too many
pass, guessing wins, so the challenge goes back to repair.

Both counts are written by arena code after a nonce the author never sees, so the solver can't forge them.
"""

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
