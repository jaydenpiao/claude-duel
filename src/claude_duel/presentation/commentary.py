"""Human-readable match commentary: a pure presentation layer over the event log.

It folds events with ``domain.events.apply`` (the same code the engine and replay use), so
it can't disagree with the match. Output is plain text; every snippet is redacted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

from claude_duel.domain.flags import scrub
from claude_duel.domain.models import Challenge, Event, MatchState, Stage
from claude_duel.presentation.common import (
    MatchFold,
    agent_cost,
    attempt_view,
    board,
    defense,
    handicap_text,
    result,
    snippet,
    spend,
    verdict,
    verdict_text,
)

__all__ = ["Commentary", "scrub", "snippet"]

RULE = "─" * 48


class Commentary(MatchFold):
    """EventObserver that writes match commentary to ``out``.

    ``lifetime_usd`` reads total spend on the key (the shared ledger) for the burn line.
    """

    def __init__(self, out: TextIO, match_id: str, cap_usd: float, lifetime_usd: Callable[[], float]) -> None:
        super().__init__(match_id)
        self._out = out
        self._cap = cap_usd
        self._lifetime = lifetime_usd

    # -- lines -----------------------------------------------------------
    def _say(self, line: str = "") -> None:
        self._out.write(line + "\n")
        self._out.flush()

    def _created(self, s: MatchState, e: Event) -> None:
        a, b = s.agents
        self._say(f"claude duel · match {s.match_id}")
        self._say(f"{a.name} ({a.model}) vs {b.name} ({b.model})")
        for agent in s.agents:
            if agent.handicap.is_set():
                self._say(f"handicap {agent.name}: {handicap_text(agent.handicap)}")
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
        ch, title, _ = verdict(s, e)
        self._say(f"arena check on {ch.author}'s {title!r}: {verdict_text(s, e)}")

    def _attempted(self, s: MatchState, e: Event) -> None:
        a = attempt_view(s, e)
        res = f"captured the flag ✓ +{a.award}" if a.captured else "no flag"
        self._say(f"{a.attacker} attacks {a.title!r} (try {a.try_no}/{s.max_attempts(a.attacker)})… {res}")

    def _attacked(self, s: MatchState, e: Event) -> None:
        held = defense(s, e)
        if held is not None:
            atk, ch, award = held
            self._say(f"  → defended ({atk.reason}), {ch.author} +{award}")

    def _scored(self, s: MatchState, e: Event) -> None:
        self._say(f"scoreboard after round {e.data['round']}: {board(s)}")
        self._say(f"spend: {self._spend(s)}")
        self._costs(s)

    def _flagged(self, s: MatchState, e: Event) -> None:
        f = s.findings[-1]
        self._say(f"  ⚑ finding ({f.severity}): {f.agent} · {f.category} · {f.rule_id} (logged, not scored)")

    def _recovered(self, s: MatchState, e: Event) -> None:
        self._say("resuming where the match stopped")

    def _failed(self, s: MatchState, e: Event) -> None:
        self._say(f"match failed during {s.failed_stage}: {scrub(s.failure or '')}")
        self._say(f"resume with: duel resume {s.match_id}")

    def _final(self, s: MatchState) -> None:
        self._say()
        self._say(RULE)
        self._say(result(s))
        u = s.usage
        self._say(f"{u.calls} model calls · in {u.input_tokens:,} · out {u.output_tokens:,} tokens · {self._spend(s)}")
        self._costs(s)

    def _costs(self, s: MatchState) -> None:
        for a in s.agents:
            self._say(f"  {a.name}: {agent_cost(s, a.name)}")

    def _spend(self, s: MatchState) -> str:
        return spend(s, self._lifetime(), self._cap)
