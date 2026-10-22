"""R3-5: encodings the regex enumeration missed are caught by assert_no_flag and redact (property tests)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from claude_duel.domain.errors import FlagLeakError
from claude_duel.domain.flags import REDACTED, assert_no_flag, derive_flag, redact

FLAG = derive_flag("secret", "m1-r1-alice-0")
INNER = FLAG[5:-1]
secrets = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=4, max_size=12)
joiners = st.text(alphabet="ZQXzqx-_ ,:", min_size=1, max_size=3)
filler = st.sampled_from("0123456789abcdefXYZ")


def _leaks(text: str) -> bool:
    try:
        assert_no_flag(text, FLAG, "test")
    except FlagLeakError:
        return True
    return False


def _flag(secret: str) -> str:
    return derive_flag(secret, "c")


@given(secrets, st.integers(min_value=2, max_value=8), joiners)
def test_chunks_with_any_joiner(secret: str, size: int, joiner: str) -> None:
    inner = _flag(secret)[5:-1]
    text = joiner.join(inner[i : i + size] for i in range(0, len(inner), size))
    with pytest.raises(FlagLeakError):
        assert_no_flag(text, _flag(secret), "test")


@given(secrets, filler)
def test_interleaved_filler(secret: str, pad: str) -> None:
    text = "".join(c + pad for c in _flag(secret)[5:-1])
    with pytest.raises(FlagLeakError):
        assert_no_flag(text, _flag(secret), "test")


@given(secrets, st.integers(min_value=-5, max_value=5).filter(bool))
def test_shifted(secret: str, k: int) -> None:
    text = "".join(chr(ord(c) + k) for c in _flag(secret)[5:-1])
    with pytest.raises(FlagLeakError):
        assert_no_flag(text, _flag(secret), "test")


@settings(max_examples=30)
@given(secrets, st.integers(min_value=1, max_value=255))
def test_xor_hex(secret: str, key: int) -> None:
    text = bytes(b ^ key for b in _flag(secret).encode()).hex().upper()
    with pytest.raises(FlagLeakError):
        assert_no_flag(text, _flag(secret), "test")


def test_decimal_codes_and_redact_withholds() -> None:
    text = ",".join(str(ord(c)) for c in INNER)
    assert _leaks(text)
    assert redact(f"out: {text}", (FLAG,)) == REDACTED


@given(st.text(alphabet="0123456789abcdef \n", max_size=400))
def test_unrelated_hex_is_not_a_leak(text: str) -> None:
    assert not _leaks(text) or INNER[:16] in text.replace(" ", "").replace("\n", "")


def test_half_digest_still_caught_but_short_slices_pass() -> None:
    assert _leaks("Z".join(INNER[:16]))
    assert not _leaks(INNER[:15])
