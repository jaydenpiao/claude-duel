"""Scripted bot for the $0 no-fluke harness: cracks a challenge with a fixed chance (drawn once per challenge).

Both bots on a board build the same fake template (chosen by the board's ``build_seed``, never the agent name), so
the only skill gap is the capture chance. Each challenge draws once from the bot's own seeded ``random.Random`` (a
miss misses every try, as a bot that cannot see the trick would);
a run is reproducible because the engine's call order is deterministic. Only ``app/harness.py`` wires it.
"""

from __future__ import annotations

import json
import random
import re

from claude_duel.adapters.providers.fake import TEMPLATES, FakeProvider
from claude_duel.ports import Completion, CompletionRequest


class ScriptedProvider(FakeProvider):
    name = "scripted"

    def __init__(self, capture_pct: int, seed: str, build_seed: str) -> None:
        super().__init__()
        self._pct = capture_pct
        self._rng = random.Random(seed)  # noqa: S311 - a reproducible simulation draw, not a secret
        self._build_seed = build_seed
        self._cracked: dict[str, bool] = {}

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose == "build":
            prompt = re.sub(r"^AGENT: \S+$", f"AGENT: {self._build_seed}", request.prompt, flags=re.M)
            return super().complete(request.model_copy(update={"prompt": prompt}))
        if request.purpose != "attack":
            return super().complete(request)
        challenge = re.search(r"^CHALLENGE: (\S+)$", request.prompt, re.M)
        key = challenge.group(1) if challenge else ""
        hit = self._cracked.get(key) if key else None
        if hit is None:
            hit = self._rng.random() * 100 < self._pct
            if key:
                self._cracked[key] = hit
        known = next((t.payload for t in TEMPLATES if t.source in request.prompt), None)
        payload = known if hit and known is not None else "scripted-miss"
        return self._wrap(request, "```json\n" + json.dumps({"payload": payload}) + "\n```")
