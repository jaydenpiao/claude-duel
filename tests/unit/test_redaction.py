"""One shared redactor and red-team PR #23 regressions: format strings, flag variants, class statements."""

from __future__ import annotations

import base64
import logging

import pytest

from claude_duel.domain import policy
from claude_duel.domain.errors import FlagLeakError
from claude_duel.domain.flags import SECRET_PATTERNS, assert_no_flag, derive_flag, redact, scrub
from claude_duel.observability import JsonFormatter

FLAG = derive_flag("secret", "c1")
INNER = FLAG[5:-1]


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


VARIANTS = {
    "literal": FLAG,
    "reversed": FLAG[::-1],
    "inner": INNER,
    "inner reversed": INNER[::-1],
    "upper": FLAG.upper(),
    "hex": FLAG.encode().hex(),
    "base64": _b64(FLAG),
    "base64 no padding": _b64(FLAG).rstrip("="),
    "base64 inner": _b64(INNER),
    "base32": base64.b32encode(FLAG.encode()).decode(),
    "spaced": " ".join(FLAG),
    "split lines": "\n".join(INNER[i : i + 4] for i in range(0, 32, 4)),
    "dashed": "-".join(INNER),
    "first half": "x " + INNER[:16] + " y",
    "second half reversed": INNER[16:][::-1],
}


@pytest.mark.parametrize("text", VARIANTS.values(), ids=VARIANTS.keys())
def test_variants_are_caught(text: str) -> None:
    with pytest.raises(FlagLeakError):
        assert_no_flag(f"wrong input, but: {text}", FLAG, "attack prompt")
    out = redact(f"out: {text} end", (FLAG,))
    assert "<redacted>" in out
    assert_no_flag(out, FLAG, "redacted output")  # nothing derived survives


def test_unrelated_text_passes() -> None:
    assert_no_flag("no flag here, just DUEL and 0123 and a sentence", FLAG, "prompt")
    assert redact("hello world", (FLAG,)) == "hello world"


SECRETS = {
    "openai key": "sk-abcdefgh12345678",  # gitleaks:allow (fake test value)
    "api_key=": "api_key=hunter2",
    "authorization header": "Authorization: Bearer xyz",
    "long hex": "ab" * 16,
    "unknown flag": "DUEL{" + "f" * 32 + "}",
}


@pytest.mark.parametrize("secret", SECRETS.values(), ids=SECRETS.keys())
def test_scrub_is_the_union_of_both_old_redactors(secret: str) -> None:
    assert secret not in scrub(f"x {secret} y")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, f"msg {secret}", None, None)
    assert secret not in JsonFormatter().format(record)
    assert len(SECRET_PATTERNS) == 3


def test_scrub_with_known_flag_catches_variants() -> None:
    assert FLAG[::-1] not in scrub(FLAG[::-1], (FLAG,))


FORMAT_BYPASSES = {
    "chr-built field + format": 'u = "_" * 2\nb = chr(123) + "0." + u + "class" + u + chr(125)\nprint(b.format(len))\n',
    "format_map": 'print("{x}".format_map({"x": 1}))\n',
    "string.Formatter": 'import string\nprint(string.Formatter().vformat("{0}", (1,), {}))\n',
    "from string import Formatter": "from string import Formatter\nprint(Formatter)\n",
}


@pytest.mark.parametrize("source", FORMAT_BYPASSES.values(), ids=FORMAT_BYPASSES.keys())
def test_format_paths_rejected(source: str) -> None:
    reason = policy.check_source(source, 4096)
    assert reason is not None
    assert "format" in reason.lower() or "Formatter" in reason


def test_class_statements_rejected() -> None:
    reason = policy.check_source("class A:\n    def __init_subclass__(cls):\n        pass\n", 4096)
    assert reason is not None
    assert reason.startswith("class statements not allowed")
    assert policy.reason_code(reason) == "class"
    assert policy.check_source('import sys\nprint(f"{1 + 1}")\n', 4096) is None
