"""Public projection of events (ADR 0012): what ``duel sync`` may store and export. Pure.

An allowlist per event kind keeps only facts (ids, rounds, scores, tokens, cost, verdicts). Challenge source,
reference payloads, solvers, attack payloads and sandbox output, scratchpad notes and finding excerpts are
dropped. Free text is never stored: sync can't know the flags (it never opens ``.secret``), so a string is kept
only if it is short and token-shaped (ids, models, verdicts, stage names), then scrubbed; anything else, such as a
build error, failure reason or challenge title, becomes ``FREE_TEXT``. Unknown kinds keep nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from claude_duel.domain import events as ev
from claude_duel.domain.flags import scrub
from claude_duel.domain.models import Event

_Json = dict[str, object]

_KEEP: Mapping[str, tuple[str, ...]] = {
    ev.MATCH_CREATED: ("agents", "rules", "limits"),
    ev.STAGE_CHANGED: ("to",),
    ev.MATCH_FAILED: ("reason",),
    ev.MATCH_RECOVERED: (),
    ev.CALL_CHARGED: ("agent", "model", "tokens", "usd", "elapsed_ms", "input_tokens", "output_tokens"),
    ev.BUILD_REJECTED: ("challenge_id", "error"),
    ev.CHALLENGE_BUILT: ("challenge",),
    ev.CHALLENGE_VALIDATED: ("validation",),
    ev.ATTACK_STARTED: ("key", "try_no"),
    ev.ALLOCATE_STARTED: ("agent", "round"),
    ev.ATTACK_ATTEMPTED: ("key", "attempt"),
    ev.CHALLENGE_ATTACKED: ("key", "attack"),
    ev.ROUND_SCORED: ("round", "delta"),
    ev.FINDING_FLAGGED: ("finding",),
    ev.NOTE_RECORDED: ("agent", "round"),
}
# Nested objects lose these keys wherever they appear.
_DROP = frozenset({"source", "reference_payload", "solver", "payload", "output", "excerpt", "text"})
_SPEC_KEEP = ("difficulty", "probe_solved")
FREE_TEXT = "<text omitted>"
_TOKEN = re.compile(r"[\w.:/@+\-]{0,64}")  # one token, no spaces or braces


def public_text(value: str) -> str:
    """A string safe to store without knowing the flags: a scrubbed token, else ``FREE_TEXT``."""
    return scrub(value) if _TOKEN.fullmatch(value) else FREE_TEXT


def public_data(event: Event) -> _Json:
    """The event payload reduced to its allowlisted, scrubbed fields."""
    kept = {k: v for k, v in event.data.items() if k in _KEEP.get(event.type, ())}
    challenge = kept.get("challenge")
    if isinstance(challenge, dict):
        spec = challenge.get("spec")
        kept["challenge"] = challenge | {"spec": _pick(spec, _SPEC_KEEP) if isinstance(spec, dict) else None}
    return cast(_Json, _clean(kept))


def _pick(data: Mapping[str, object], keys: tuple[str, ...]) -> _Json:
    return {k: data[k] for k in keys if k in data}


def _clean(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if k not in _DROP}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, str):
        return public_text(value)
    return value
