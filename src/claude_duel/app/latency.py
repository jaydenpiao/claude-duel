"""Provider latency per model, measured on real matches, so timeouts are sized for the slowest model in a plan.

``MEASURED`` is pinned from the real match logs of 2026-10-30 to 2026-11-19 (calls whose agent ran with an output cap
of at least 8000 tokens; `scripts/calibrate_usage.py` prints the same columns). A model with no logged calls uses a
named proxy, else the slowest measured model, so an unknown model is never given a short timeout. No API calls.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from claude_duel.app.budget import FAKE_MODEL

# seconds per call: (p95, max observed)
MEASURED: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (214.5, 214.5),
    "claude-haiku-5-5": (35.5, 39.2),
    "claude-opus-5-5": (640.3, 640.3),
    "gpt-6-astra": (314.0, 781.7),
    "gpt-6-luna": (14.6, 28.8),
}
PROXY: dict[str, str] = {
    "claude-fable-5-2": "claude-fable-5-1",
    "claude-sonnet-5-5": "claude-opus-5-5",
    "gpt-6.2-astra": "gpt-6-astra",
    "gpt-6.2-sol": "gpt-6-astra",
    "gpt-6.1-terra": "gpt-6-astra",
}
# Floors (fairness audit R7): even fast models get room for a slow day; never below these.
MIN_PROVIDER_TIMEOUT_S = 300
MIN_CALL_DEADLINE_S = 600
MIN_MATCH_DEADLINE_S = 3600
HEADROOM = 1.15  # over the slowest call ever seen
CALLS_PER_HALF = 9  # both agents, one half of a ladder board (about 4.3 measured calls each, rounded up)


@dataclass(frozen=True)
class Timeouts:
    provider_timeout_s: int  # one HTTP request
    call_deadline_s: int  # one call, retries included
    match_deadline_s: int  # recorded provider time per half (Rules.match_deadline_s)


def latency(model: str) -> tuple[float, float]:
    """(p95, max) seconds for ``model``: measured, its proxy's, or the slowest measured model's."""
    if model == FAKE_MODEL:
        return 0.0, 0.0
    source = model if model in MEASURED else PROXY.get(model, "")
    if source in MEASURED:
        return MEASURED[source]
    return max(MEASURED.values(), key=lambda row: row[1])


def _minutes(seconds: float) -> int:
    return int(math.ceil(seconds / 60) * 60)


def sized(models: Iterable[str]) -> Timeouts:
    """Timeouts for the slowest model in ``models``: an HTTP timeout over its slowest call seen (with headroom), a
    call deadline that allows one retry after a full timeout, and a match deadline for a half of its p95 calls."""
    rows = [latency(m) for m in models] or [(0.0, 0.0)]
    p95, worst = max(r[0] for r in rows), max(r[1] for r in rows)
    timeout = max(MIN_PROVIDER_TIMEOUT_S, _minutes(worst * HEADROOM))
    return Timeouts(
        provider_timeout_s=timeout,
        call_deadline_s=max(MIN_CALL_DEADLINE_S, 2 * timeout),
        match_deadline_s=max(MIN_MATCH_DEADLINE_S, _minutes(CALLS_PER_HALF * p95 * HEADROOM)),
    )
