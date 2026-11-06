"""Flag derivation, leak guards and the one shared redactor (``scrub``)."""

from __future__ import annotations

import base64
import codecs
import hashlib
import hmac
import re
from collections.abc import Iterable
from functools import lru_cache
from typing import TYPE_CHECKING

from claude_duel.domain.errors import FlagLeakError

if TYPE_CHECKING:
    from claude_duel.domain.models import MatchState

FLAG_PATTERN = re.compile(r"DUEL\{[0-9a-f]{8,64}\}")
REDACTED = "DUEL{<redacted>}"
# Flag-shaped text in forms the literal pattern misses, for checks that don't know the flag (``duel sync`` on old
# logs): rot13 (``QHRY{...}``, hex a-f becomes n-s) and decimal byte lists (``68,85,69,76,123,...``).
_DEC_SEP = r"[^0-9]{1,3}"
ENCODED_FLAG_PATTERNS = (
    re.compile(r"(?i)QHRY\{[0-9n-s]{8,64}\}"),
    re.compile(_DEC_SEP.join(("68", "85", "69", "76", "123")) + rf"(?:{_DEC_SEP}[0-9]{{2,3}}){{8,65}}"),
)
# Key-, token- and secret-shaped text: OpenAI keys, ``api_key=...``/``authorization: ...``, long hex runs.
# ``KEY_PATTERNS`` alone is the backup's key scan: event hashes match the long-hex rule.
KEY_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization)[\"':= ]+\S+"),
)
HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{32,}\b")
SECRET_PATTERNS = (*KEY_PATTERNS, HEX_PATTERN)
_GAP = r"[\W_]*"  # spaces, punctuation or newlines a challenge may put between characters
MIN_PART = 16  # halves of the 32-char digest are still a leak


FRAGMENT = 8  # SV-2: a reference payload may not share this many digest chars in a row with the flag


def shares_fragment(text: str, flag: str, size: int = FRAGMENT) -> bool:
    """Whether ``text`` contains any ``size``-char window of the flag's digest (case-insensitive)."""
    inner, low = _inner(flag).lower(), text.lower()
    return any(inner[i : i + size] in low for i in range(len(inner) - size + 1))


def derive_flag(secret: str, challenge_id: str) -> str:
    digest = hmac.new(secret.encode(), challenge_id.encode(), hashlib.sha256).hexdigest()
    return f"DUEL{{{digest[:32]}}}"


def flag_of(secret: str, cid: str, epoch: int = 0) -> str:
    """Flag of challenge ``cid`` built on repair try ``epoch``; epoch 0 is the original derivation."""
    return derive_flag(secret, cid if epoch == 0 else f"{cid}~{epoch}")


def _inner(flag: str) -> str:
    m = re.fullmatch(r"DUEL\{(.+)\}", flag)
    return m.group(1) if m else flag


def _texts(flag: str) -> tuple[list[str], list[str]]:
    """(case-insensitive forms, case-sensitive forms) a challenge could print instead of the flag."""
    inner = _inner(flag)
    parts = [inner[i : i + MIN_PART] for i in range(0, len(inner) - MIN_PART + 1, MIN_PART)]
    loose = [flag, inner, flag[::-1], inner[::-1], flag.encode().hex(), *parts, *(p[::-1] for p in parts)]
    loose += [codecs.encode(t, "rot13") for t in (flag, inner)]
    strict = [
        enc(raw.encode()).decode().rstrip("=")
        for raw in (flag, inner)
        for enc in (base64.b64encode, base64.urlsafe_b64encode, base64.b32encode)
    ]
    return loose, strict


def _spread(text: str) -> str:
    return _GAP.join(re.escape(c) for c in text if c.isalnum())


@lru_cache(maxsize=256)
def _variant_pattern(flag: str) -> re.Pattern[str]:
    loose, strict = _texts(flag)
    alts = [f"(?i:{_spread(t)})" for t in sorted(set(loose), key=len, reverse=True)]
    alts += [r"\s*".join(re.escape(c) for c in t) for t in sorted(set(strict), key=len, reverse=True)]
    return re.compile("|".join(alts))


_SHIFTS = 5  # Caesar-style +-1..5 per character
_DEC = re.compile(r"\d{2,3}")


@lru_cache(maxsize=256)
def _needles(flag: str) -> frozenset[str]:
    """every MIN_PART-char window of the digest under transforms a regex can't enumerate."""
    inner = _inner(flag)
    forms = [inner, inner[::-1], flag.encode().hex(), inner.encode().hex()]
    forms += ["".join(chr(ord(c) + k) for c in inner).lower() for k in range(-_SHIFTS, _SHIFTS + 1) if k]
    forms += [bytes(b ^ key for b in raw).hex() for raw in (flag.encode(), inner.encode()) for key in range(1, 256)]
    return frozenset(f[i : i + MIN_PART] for f in forms for i in range(len(f) - MIN_PART + 1))


def _canonical(text: str) -> list[str]:
    """Order-preserving reductions of ``text``: separators, filler every other char and decimal codes removed."""
    low = text.lower()
    hexes = re.sub(r"[^0-9a-f]", "", low)
    codes = "".join(chr(int(n)) for n in _DEC.findall(text) if 32 <= int(n) < 127)
    return [hexes, hexes[::2], hexes[1::2], re.sub(r"\s", "", low), re.sub(r"[^0-9a-f]", "", codes.lower())]


def _canonical_leak(text: str, flag: str) -> bool:
    needles = _needles(flag)
    return any(f[i : i + MIN_PART] in needles for f in _canonical(text) for i in range(len(f) - MIN_PART + 1))


def assert_no_flag(text: str, flag: str, where: str) -> None:
    """Raise if ``flag`` or a derived form is in text: reversed, hex, base64/32, rot13, case-changed,
    spaced, split (also with alphanumeric joiners or interleaved filler), shifted, XOR-hex or as decimal codes."""
    if flag in text or _variant_pattern(flag).search(text) or _canonical_leak(text, flag):
        raise FlagLeakError(f"flag would leak via {where}")


def redact(text: str, flags: tuple[str, ...] = ()) -> str:
    for f in flags:
        text = _variant_pattern(f).sub(REDACTED, text.replace(f, REDACTED))
        if _canonical_leak(text, f):
            return REDACTED  # an encoding with no clean span to cut out; withhold the whole text
    for pat in (FLAG_PATTERN, *ENCODED_FLAG_PATTERNS):
        text = pat.sub(REDACTED, text)
    return text


def flag_shaped(text: str) -> bool:
    """Whether text holds anything shaped like a flag, plain or encoded, without knowing the flag."""
    return any(p.search(text) for p in (FLAG_PATTERN, *ENCODED_FLAG_PATTERNS))


def scrub(text: str, flags: tuple[str, ...] = ()) -> str:
    """The redactor for anything shown or logged: flags (and their variants when known) plus secret-shaped text."""
    text = redact(text, flags)
    for pat in SECRET_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text


def match_flags(state: MatchState, extra_cids: Iterable[str] = ()) -> list[str]:
    """Every flag the match ever used: each challenge (plus ``extra_cids``), each repair try and the built epoch.

    The one source for the engine's event guard and the backup scan (review M20)."""
    if not state.secret:
        return []
    cids = set(state.challenges) | set(state.build_errors) | set(extra_cids)
    out: list[str] = []
    for cid in sorted(cids):
        ch = state.challenges.get(cid)
        last = max(len(state.build_errors.get(cid, [])), ch.flag_epoch if ch else 0)
        out += [flag_of(state.secret, cid, epoch) for epoch in range(last + 1)]
    return out
