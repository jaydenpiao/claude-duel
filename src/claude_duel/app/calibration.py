"""Calibrated cost basis: per-model token use measured on real matches, so a plan is priced from what each model
actually writes instead of one assumed reply size for every model.

``MEASURED`` is pinned from the real match logs of 2026-10-30 to 2026-11-19 by ``scripts/calibrate_usage.py`` (calls
whose agent ran with an output cap of at least 8000 tokens). The logs are the maintainer's local run store (the
``*.events.jsonl`` files under his ``DUEL_STORE_DIR``) and are not in the repo; ``tests/unit/test_calibrate_usage.py``
pins the script's arithmetic on a fixture. Models with no logged calls borrow a measured model's
row (``PROXY``) and say so; a model with neither is refused, never guessed. No API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from claude_duel.app.budget import DEFAULT_PRICES, FAKE_MODEL
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import Rules

Effort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Usage:
    input_tokens: int  # mean per call
    output_tokens: int  # mean per call
    calls: int  # calls measured
    source: str  # the model the numbers were measured on


# mean tokens per call: (input, output, calls measured)
MEASURED: dict[str, tuple[int, int, int]] = {
    "claude-fable-5-1": (1378, 8996, 18),
    "claude-haiku-5-5": (1307, 2861, 97),
    "claude-opus-5-5": (1410, 3384, 10),
    "gpt-6-astra": (988, 3433, 26),
    "gpt-6-luna": (916, 705, 105),
}
# no logged calls of their own: priced as the nearest measured model of the same vendor and tier or above
PROXY: dict[str, str] = {
    "claude-fable-5-2": "claude-fable-5-1",
    "claude-sonnet-5-5": "claude-opus-5-5",
    "gpt-6.2-astra": "gpt-6-astra",
    "gpt-6.2-sol": "gpt-6-astra",
    "gpt-6.1-terra": "gpt-6-astra",
}
# the effort each MEASURED row was logged at: none was sent, so Claude ran at its API default (high), gpt-6-astra
# at OpenAI's (medium) and gpt-6-luna at the adapter's small-model default (low)
LOGGED_EFFORT: dict[str, Effort] = {
    "claude-fable-5-1": "high",
    "claude-haiku-5-5": "high",
    "claude-opus-5-5": "high",
    "gpt-6-astra": "medium",
    "gpt-6-luna": "low",
}
# output tokens relative to medium; an assumption (no row is logged at two efforts yet): re-pin after the pilot,
# which logs haiku and luna at high
EFFORT_OUTPUT: dict[Effort, float] = {"low": 0.5, "medium": 1.0, "high": 2.0}
# paid calls per agent per challenge (build, format retries, attack tries): the largest per-model mean measured
# (gpt-6-astra 3.33; haiku 2.59, luna 2.79, fable 2.17), the same for every model so no side is priced lighter
CALLS_PER_CHALLENGE = 3.33


def usage(model: str) -> Usage:
    """The measured row for ``model`` (or its proxy); ``ConfigError`` when neither exists."""
    if model == FAKE_MODEL:
        return Usage(0, 0, 0, FAKE_MODEL)
    source = model if model in MEASURED else PROXY.get(model, "")
    if source not in MEASURED:
        raise ConfigError(f"no calibrated token use for {model!r}: add it to calibration.MEASURED or PROXY")
    tin, tout, calls = MEASURED[source]
    return Usage(tin, tout, calls, source)


def effort_scale(source: str, effort: Effort | None) -> float:
    """Output tokens at ``effort`` over those logged for ``source`` (1.0 when no effort is set)."""
    if effort is None or source not in LOGGED_EFFORT:
        return 1.0
    return EFFORT_OUTPUT[effort] / EFFORT_OUTPUT[LOGGED_EFFORT[source]]


def calibrated_agent_usd(rules: Rules, model: str) -> float:
    """One agent's expected cost for one match (one half of a board) on the calibrated basis: measured mean tokens per
    call at ``DEFAULT_PRICES[model]``, ``CALLS_PER_CHALLENGE`` calls per challenge plus one allocation per round in
    ledger modes. Output is scaled from the logged effort to ``rules.reasoning_effort`` (``EFFORT_OUTPUT``) and
    capped at the rules' largest per-purpose output cap."""
    use = usage(model)
    pin, pout = DEFAULT_PRICES[model]
    out = min(
        use.output_tokens * effort_scale(use.source, rules.reasoning_effort),
        max(rules.output_cap(purpose=p) for p in ("build", "attack", "allocate")),
    )
    calls = rules.rounds * rules.challenges_per_round * CALLS_PER_CHALLENGE
    calls += rules.rounds if rules.ledger is not None else 0
    return calls * (use.input_tokens * pin + out * pout) / 1_000_000
