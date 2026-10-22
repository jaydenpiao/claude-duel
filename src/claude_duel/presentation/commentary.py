"""Human-readable match commentary: a pure presentation layer over the event log.

It folds events with ``domain.events.apply`` (the same code the engine and replay use), so
it can't disagree with the match. Output is plain text; every snippet is redacted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TextIO

from claude_duel.domain import events as ev
from claude_duel.domain.flags import redact
from claude_duel.domain.models import Attack, Challenge, Event, MatchState, Stage, Validation
from claude_duel.domain.scoring import defense_award, solve_award

SNIPPET_LINES = 3
SNIPPET_WIDTH = 72
_SECRETISH = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|[0-9a-f]{32,})\b")
RULE = "─" * 48


def scrub(text: str) -> str:
    """Redact flags and anything key- or secret-shaped."""
    return _SECRETISH.sub("<redacted>", redact(text))


def snippet(source: str) -> list[str]:
    lines = [ln.rstrip() for ln in source.strip().splitlines() if ln.strip()]
    shown = [scrub(ln)[:SNIPPET_WIDTH] for ln in lines[:SNIPPET_LINES]]
    if len(lines) > SNIPPET_LINES:
        shown.append(f"… (+{len(lines) - SNIPPET_LINES} lines)")
    return shown


class Commentary:
    """EventObserver that writes match commentary to ``out``.

    ``lifetime_usd`` reads total spend on the key (the shared ledger) for the burn line.
    """

    def __init__(self, out: TextIO, match_id: str, key_limit_usd: float, lifetime_usd: Callable[[], float]) -> None:
        self._out = out
        self._match_id = match_id
        self._key_limit = key_limit_usd
        self._lifetime = lifetime_usd
        self._state: MatchState | None = None
        self._handlers: dict[str, Callable[[MatchState, Event], None]] = {
            ev.MATCH_CREATED: self._created,
            ev.STAGE_CHANGED: self._stage,
            ev.CHALLENGE_BUILT: self._built,
            ev.CHALLENGE_VALIDATED: self._validated,
            ev.ATTACK_ATTEMPTED: self._attempted,
            ev.CHALLENGE_ATTACKED: self._attacked,
            ev.ROUND_SCORED: self._scored,
            ev.MATCH_FAILED: self._failed,
            ev.MATCH_RECOVERED: lambda s, e: self._say("resuming where the match stopped"),
        }

    @property
    def state(self) -> MatchState | None:
        return self._state

    def __call__(self, event: Event) -> None:
        if self._state is None:
            self._state = ev.initial_state(self._match_id, event)
        ev.apply(self._state, event)
        handler = self._handlers.get(event.type)
        if handler is not None:
            handler(self._state, event)

    # -- lines -----------------------------------------------------------
    def _say(self, line: str = "") -> None:
        self._out.write(line + "\n")
        self._out.flush()

    def _created(self, s: MatchState, e: Event) -> None:
        a, b = s.agents
        self._say(f"claude duel · match {s.match_id}")
        self._say(f"{a.name} ({a.model}) vs {b.name} ({b.model})")
        r = s.rules
        self._say(
            f"{r.rounds} rounds · {r.challenges_per_round} challenges each per round · up to {r.max_attempts} tries"
        )

    def _stage(self, s: MatchState, e: Event) -> None:
        if s.stage is Stage.BUILD:
            self._say()
            self._say(f"{RULE}\nround {s.round}/{s.rules.rounds}\n{RULE}")
        elif s.stage is Stage.COMPLETE:
            self._final(s)

    def _built(self, s: MatchState, e: Event) -> None:
        ch = Challenge.model_validate(e.data["challenge"])
        if ch.spec is None:
            self._say(f"{ch.author} forfeited a challenge: {scrub(ch.forfeit_reason or 'no valid answer')}")
            return
        self._say(f"{ch.author} built a challenge guarding the arena's flag: {scrub(ch.spec.title)}")
        self._say(f"  difficulty {ch.spec.difficulty} · {len(ch.spec.source.splitlines())} lines")
        for line in snippet(ch.spec.source):
            self._say(f"  │ {line}")

    def _validated(self, s: MatchState, e: Event) -> None:
        v = Validation.model_validate(e.data["validation"])
        ch = s.challenges[v.challenge_id]
        title = scrub(ch.spec.title) if ch.spec else "forfeit"
        verdict = "solvable ✓" if v.valid else f"rejected ✗ ({scrub(v.reason)}) -{s.rules.invalid_penalty * ch.round}"
        self._say(f"arena check on {ch.author}'s {title!r}: {verdict}")

    def _attempted(self, s: MatchState, e: Event) -> None:
        key = str(e.data["key"])
        cid, _, attacker = key.rpartition(":")
        n = len(s.attempts[key])
        ch = s.challenges[cid]
        assert ch.spec is not None, "only valid challenges are attacked"
        if s.attempts[key][-1].captured:
            result = f"captured the flag ✓ +{solve_award(ch.spec, ch.round, n, s.rules)}"
        else:
            result = "no flag"
        self._say(f"{attacker} attacks {scrub(ch.spec.title)!r} (try {n}/{s.rules.max_attempts})… {result}")

    def _attacked(self, s: MatchState, e: Event) -> None:
        atk = Attack.model_validate(e.data["attack"])
        ch = s.challenges[atk.challenge_id]
        assert ch.spec is not None, "only valid challenges are attacked"
        if not atk.solved:
            self._say(f"  → defended ({atk.reason}), {ch.author} +{defense_award(ch.spec, ch.round, s.rules)}")

    def _scored(self, s: MatchState, e: Event) -> None:
        self._say(f"scoreboard after round {e.data['round']}: {self._board(s)}")
        self._say(f"spend: {self._spend(s)}")

    def _failed(self, s: MatchState, e: Event) -> None:
        self._say(f"match failed during {s.failed_stage}: {scrub(s.failure or '')}")
        self._say(f"resume with: duel resume {s.match_id}")

    def _final(self, s: MatchState) -> None:
        self._say()
        self._say(RULE)
        ranked = sorted(s.scores.items(), key=lambda kv: -kv[1])
        if len(ranked) == 2 and ranked[0][1] == ranked[1][1]:
            self._say(f"draw: {self._board(s)}")
        else:
            self._say(f"winner: {ranked[0][0]} · {self._board(s)}")
        self._say(f"{s.usage.calls} model calls · {s.usage.tokens} tokens · {self._spend(s)}")

    @staticmethod
    def _board(s: MatchState) -> str:
        return "  ".join(f"{a.name} {s.scores.get(a.name, 0)}" for a in s.agents)

    def _spend(self, s: MatchState) -> str:
        return f"${s.usage.usd:.4f} this match · ${self._lifetime():.4f} of ${self._key_limit:g} burned"
