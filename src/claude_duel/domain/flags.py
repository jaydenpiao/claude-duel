"""Flag derivation, leak guards and the one shared redactor (``scrub``)."""

import base64
import codecs
import hashlib
import hmac
import re
from functools import lru_cache

from claude_duel.domain.errors import FlagLeakError

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
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization)[\"':= ]+\S+"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
)
_GAP = r"[\W_]*"  # spaces, punctuation or newlines a challenge may put between characters
MIN_PART = 16  # halves of the 32-char digest are still a leak


def derive_flag(secret: str, challenge_id: str) -> str:
    digest = hmac.new(secret.encode(), challenge_id.encode(), hashlib.sha256).hexdigest()
    return f"DUEL{{{digest[:32]}}}"


def flag_of(secret: str, cid: str, epoch: int = 0) -> str:
    """Flag of challenge ``cid`` built on repair try ``epoch`` (R3-2); epoch 0 is the original derivation."""
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
    alts += [_DEC_SEP.join(str(b) for b in t.encode()) for t in (flag, _inner(flag))]  # decimal byte lists
    return re.compile("|".join(alts))


def assert_no_flag(text: str, flag: str, where: str) -> None:
    """Raise if ``flag`` or a derived form (reversed, hex, base64/32, rot13, decimal bytes, case-changed, spaced or
    split) is in text."""
    if flag in text or _variant_pattern(flag).search(text):
        raise FlagLeakError(f"flag would leak via {where}")


def redact(text: str, flags: tuple[str, ...] = ()) -> str:
    for f in flags:
        text = _variant_pattern(f).sub(REDACTED, text.replace(f, REDACTED))
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
