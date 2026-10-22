"""Flag derivation and leak guards."""

import hashlib
import hmac
import re

from claude_duel.domain.errors import FlagLeakError

FLAG_PATTERN = re.compile(r"DUEL\{[0-9a-f]{8,64}\}")


def derive_flag(secret: str, challenge_id: str) -> str:
    digest = hmac.new(secret.encode(), challenge_id.encode(), hashlib.sha256).hexdigest()
    return f"DUEL{{{digest[:32]}}}"


def assert_no_flag(text: str, flag: str, where: str) -> None:
    if flag in text:
        raise FlagLeakError(f"flag would leak via {where}")


def redact(text: str, flags: tuple[str, ...] = ()) -> str:
    for f in flags:
        text = text.replace(f, "DUEL{<redacted>}")
    return FLAG_PATTERN.sub("DUEL{<redacted>}", text)
