"""Design rec R5: no hashes or CRCs in check(), and two more ways an answer hides in the source."""

from __future__ import annotations

import pytest

from claude_duel.domain import contract, policy
from claude_duel.domain.models import ChallengeSpec


def _spec(check_src: str, payload: str = "zq7k") -> ChallengeSpec:
    source, problem = contract.compose(check_src)
    assert problem is None, problem
    return ChallengeSpec(title="t", description="d", source=source, reference_payload=payload, difficulty=1)


@pytest.mark.parametrize(
    "line",
    [
        "import hashlib",
        "import zlib",
        "import binascii",
        "import struct",
        "from zlib import crc32",
        "from binascii import crc_hqx",
    ],
)
def test_check_form_rejects_hash_and_crc_imports(line: str) -> None:
    spec = _spec(f"{line}\n\ndef check(s):\n    return len(s) == 4\n")
    reason = policy.check(spec, 4096, "check")
    assert reason is not None
    assert "import not allowed in check()" in reason
    assert policy.check(spec, 4096, "program") is None  # legacy program form keeps them


def test_plain_check_still_passes() -> None:
    assert policy.check(_spec("import math\n\ndef check(s):\n    return len(s) == 4\n"), 4096, "check") is None


def test_affine_char_codes_are_caught() -> None:
    codes = [3 * ord(c) + 7 for c in "zq7k"]
    src = f"K = {codes}\n\ndef check(s):\n    return [3 * ord(c) + 7 for c in s] == K\n"
    reason = policy.check(_spec(src), 4096, "check")
    assert reason is not None
    assert "char codes" in reason


def test_position_map_is_caught_in_any_key_order() -> None:
    src = "M = {2: '7', 0: 'z', 3: 'k', 1: 'q'}\n\ndef check(s):\n    return all(s[i] == c for i, c in M.items())\n"
    reason = policy.check(_spec(src), 4096, "check")
    assert reason is not None
    assert "position-to-character map" in reason


def test_unrelated_int_lists_and_maps_pass() -> None:
    src = "W = [1, 2, 3, 4]\nM = {0: 'a', 1: 'b', 2: 'c', 3: 'd'}\n\ndef check(s):\n    return len(s) == 4\n"
    assert policy.check(_spec(src), 4096, "check") is None
    assert not policy._affine([5, 5, 5, 5], [1, 1, 1, 1])  # no two distinct codes: no slope to solve
