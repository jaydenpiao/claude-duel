"""Fixed validation probes: junk inputs that must not reveal the flag."""

from __future__ import annotations

import hashlib

MAX_PAYLOAD_CHARS = 4096  # longest reference or attack payload; validation probes this length too (S15-1)


def _same_class(c: str) -> str:
    """A different char of c's class (digit, lower, upper, else printable)."""
    for pool in ("0123456789", "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if c in pool:
            return pool[(pool.index(c) + 1) % len(pool)]
    return "#" if c != "#" else "%"


def _indexes(length: int, seed: str) -> tuple[int, ...]:
    """First, middle and last, plus one extra index derived from the match seed."""
    if length <= 0:
        return ()
    mid = length // 2
    extra = int.from_bytes(hashlib.sha256(f"{seed}:{length}".encode()).digest()[:4], "big") % length
    return tuple(dict.fromkeys((0, mid, length - 1, extra)))


def junk_inputs(payload: str, seed: str = "") -> list[str]:
    """Fixed, free inputs that must not reveal the flag (besides empty input): degenerate acceptance fails."""
    p = payload.strip()
    tweak = (("b" if p[0] == "a" else "a") + p[1:]) if p else ""
    mid = len(p) // 2
    same_shape = [p[:i] + _same_class(p[i]) + p[i + 1 :] for i in _indexes(len(p), seed) if p]
    probes = [" ", "a", "0", "\n", "A" * len(p), tweak, *same_shape, p[:mid], p[mid + 1 :]]
    return list(dict.fromkeys(x for x in probes if x.strip() and x.strip() != p))


def long_probe(payload: str) -> str:
    """One max-length junk input in the reference's first-char class: an honest check must answer it quickly, so a
    timed-out attack of at most this length is the check's fault, not the attacker's."""
    p = payload.strip()
    return _same_class(p[0] if p else "a") * MAX_PAYLOAD_CHARS
