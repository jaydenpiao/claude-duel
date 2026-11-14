"""Fixed validation probes: junk inputs that must not reveal the flag."""

from __future__ import annotations


def _same_class(c: str) -> str:
    """A different char of c's class (digit, lower, upper, else printable)."""
    for pool in ("0123456789", "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if c in pool:
            return pool[(pool.index(c) + 1) % len(pool)]
    return "#" if c != "#" else "%"


def junk_inputs(payload: str) -> list[str]:
    """Fixed, free inputs that must not reveal the flag (besides empty input): degenerate acceptance fails."""
    p = payload.strip()
    tweak = (("b" if p[0] == "a" else "a") + p[1:]) if p else ""
    mid = len(p) // 2
    same_shape = [p[:i] + _same_class(p[i]) + p[i + 1 :] for i in dict.fromkeys((len(p) - 1, mid)) if p]
    probes = [" ", "a", "0", "\n", "A" * len(p), tweak, *same_shape, p[:mid], p[mid + 1 :]]
    return list(dict.fromkeys(x for x in probes if x.strip() and x.strip() != p))
