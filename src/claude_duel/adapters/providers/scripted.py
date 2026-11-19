"""Scripted bot for the $0 no-fluke harness: builds like the fake provider, captures with a fixed chance per try.

Each attack try draws once from its own seeded ``random.Random``, so a run is reproducible: the engine's call
order is deterministic. Never used for a scored match; only ``app/harness.py`` wires it.
"""

from __future__ import annotations

import json
import random

from claude_duel.adapters.providers.fake import TEMPLATES, FakeProvider
from claude_duel.ports import Completion, CompletionRequest


class ScriptedProvider(FakeProvider):
    name = "scripted"

    def __init__(self, capture_pct: int, seed: str) -> None:
        super().__init__()
        self._pct = capture_pct
        self._rng = random.Random(seed)  # noqa: S311 - a reproducible simulation draw, not a secret

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose != "attack":
            return super().complete(request)
        hit = self._rng.random() * 100 < self._pct
        known = next((t.payload for t in TEMPLATES if t.source in request.prompt), None)
        payload = known if hit and known is not None else "scripted-miss"
        return self._wrap(request, "```json\n" + json.dumps({"payload": payload}) + "\n```")
