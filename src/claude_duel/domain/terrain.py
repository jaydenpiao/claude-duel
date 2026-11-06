"""Sealed Ledger seeded terrain (design-core-logic.md 4.2 step 3): a public stdin transform per challenge.

The arena applies ``apply(t, payload)`` before the challenge reads stdin, so the attacker must send
``invert(t, wanted)``. Every transform has an exact inverse; a draw whose inverse is undefined on the
author's reference payload falls back to ``identity``, so validity holds by construction.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
from collections.abc import Callable, Iterable

KINDS = ("identity", "reverse", "rot13", "swapcase", "xor", "b64", "prefix")
PAD = "~"
_SWAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)


def _xor(x: str, b: int) -> str:
    return "".join(chr(ord(c) ^ b) if ord(c) < 128 else c for c in x)


_APPLY: dict[str, Callable[[str, str], str]] = {
    "identity": lambda x, _: x,
    "reverse": lambda x, _: x[::-1],
    "rot13": lambda x, _: codecs.encode(x, "rot13"),
    "swapcase": lambda x, _: x.translate(_SWAP),
    "xor": lambda x, arg: _xor(x, int(arg, 16)),
    "b64": lambda x, _: base64.b64encode(x.encode()).decode(),
    "prefix": lambda x, arg: PAD * int(arg) + x,
}


def apply(t: str, x: str) -> str:
    kind, _, arg = t.partition(":")
    if kind not in _APPLY:
        raise ValueError(f"unknown terrain {t!r}")
    return _APPLY[kind](x, arg)


def invert(t: str, y: str) -> str | None:
    """The x with apply(t, x) == y, or None when no such x exists."""
    kind, _, arg = t.partition(":")
    if kind == "b64":
        try:
            x = base64.b64decode(y.encode(), validate=True).decode()
        except (binascii.Error, UnicodeError, ValueError):
            return None
        return x if apply(t, x) == y else None
    if kind == "prefix":
        n = int(arg)
        return y[n:] if y.startswith(PAD * n) else None
    return apply(t, y)  # the rest are involutions


def describe(t: str) -> str:
    kind, _, arg = t.partition(":")
    if kind == "xor":
        return f"xor 0x{arg}: every ASCII char of your payload is XORed with 0x{arg} before the program reads it"
    if kind == "prefix":
        return f"prefix {arg}: the program reads {PAD * int(arg)!r} + your payload"
    return {
        "identity": "none: stdin is your payload as sent",
        "reverse": "reverse: the program reads your payload reversed",
        "rot13": "rot13: ASCII letters in your payload are rot13'd before the program reads it",
        "swapcase": "swapcase: ASCII letter case in your payload is flipped before the program reads it",
        "b64": "b64: the program reads base64(utf-8 of your payload)",
    }[kind]


def draw(seed: str, round_no: int, slot: str) -> str:
    """Reproducible from (seed, round, slot) alone; slot = "<author>:<index>", independent of the match id."""
    h = hashlib.sha256(f"{seed}:{round_no}:{slot}:terrain".encode()).digest()
    kind = KINDS[h[0] % len(KINDS)]
    if kind == "xor":
        return f"xor:{1 + h[1] % 31:02x}"
    if kind == "prefix":
        return f"prefix:{1 + h[1] % 4}"
    return kind


def draw_round(seed: str, round_no: int, refs: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """challenge id -> terrain for (id, slot, reference payload); identity where the ref has no inverse."""
    out: dict[str, str] = {}
    for cid, slot, ref in sorted(refs):
        t = draw(seed, round_no, slot)
        out[cid] = t if invert(t, ref) is not None else "identity"
    return out
