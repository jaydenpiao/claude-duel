"""Vendor-neutral arena units for stakes, the token clock and output caps (``Rules.accounting = "arena"``).

Vendors tokenize the same text differently (Claude reports about 1.59x GPT's input tokens on identical prompts), so
a cap or stake counted in vendor tokens buys one vendor less text. Arena units fix one basis: input = request bytes
/ 4 (rounded up); output = the vendor's output tokens x its pinned ratio (``Rules.arena_ratio``), the same ratio that
sends output caps as ``ceil(cap / ratio)`` vendor tokens. The ratio is never taken from the call itself: a per-call
``k`` (input bytes / input tokens) let the opponent's text in the prompt, such as whitespace runs, multiply a
side's stake and clock debit 2.7-5.6x (security review S17-2, 2026-11-26). Logged units replay exactly.
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


def units(input_bytes: int, output_tokens: int, ratio: float) -> tuple[int, int]:
    """(input units, output units) for one call: bytes / 4 in, and output at the vendor's pinned ``ratio``."""
    return math.ceil(input_bytes / BYTES_PER_UNIT), math.ceil(output_tokens * ratio)


def vendor_tokens(cap_units: int, ratio: float) -> int:
    """``max_tokens`` to send for an output cap of ``cap_units`` arena units at the vendor's pinned ``ratio``."""
    return math.ceil(cap_units / ratio)
