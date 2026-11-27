"""Vendor-neutral arena units for stakes, the token clock and output caps (``Rules.accounting = "arena"``).

Vendors tokenize the same text differently (Claude reports about 1.59x GPT's input tokens on identical prompts), so
a cap or stake counted in vendor tokens buys one vendor less text. Arena units fix one basis: input = request bytes
/ 4 (rounded up); output = the vendor's output tokens x ``k``, where ``k`` = input units / reported input tokens for
that same call. That is self-calibrating per call, logged, and replays exactly. Output caps are sent as
``ceil(cap / ratio)`` vendor tokens, ``ratio`` being the vendor's pinned ``k`` (``Rules.arena_ratio``).
"""

from __future__ import annotations

import json
import math
from typing import Any

BYTES_PER_UNIT = 4


def request_bytes(system: str, prompt: str, schema: dict[str, Any] | None = None) -> int:
    """Bytes the vendor tokenizes as input: system, prompt and the answer schema."""
    extra = len(json.dumps(schema).encode()) if schema else 0
    return len(system.encode()) + len(prompt.encode()) + extra


def units(input_bytes: int, input_tokens: int, output_tokens: int) -> tuple[int, int]:
    """(input units, output units) for one call: bytes / 4 in, and output scaled by this call's own ``k``."""
    unit_in = math.ceil(input_bytes / BYTES_PER_UNIT)
    k = unit_in / input_tokens if input_tokens > 0 else 1.0
    return unit_in, math.ceil(output_tokens * k)


def vendor_tokens(cap_units: int, ratio: float) -> int:
    """``max_tokens`` to send for an output cap of ``cap_units`` arena units at the vendor's pinned ``ratio``."""
    return math.ceil(cap_units / ratio)
