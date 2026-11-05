"""Helpers shared by every renderer (commentary, arena): event folding, redaction and the
sentences both views print. Pure presentation: nothing here mutates a match the engine owns.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from claude_duel.domain import events as ev
from claude_duel.domain.flags import scrub
from claude_duel.domain.models import Attack, Challenge, Event, Handicap, MatchState, Usage, Validation
from claude_duel.domain.scoring import defense_award, solve_award
from claude_duel.domain.series import decide

SNIPPET_LINES = 3
SNIPPET_WIDTH = 72

HOOKS = {
    ev.MATCH_CREATED: "_created",
    ev.STAGE_CHANGED: "_stage",
    ev.CHALLENGE_BUILT: "_built",
    ev.CHALLENGE_VALIDATED: "_validated",
    ev.ATTACK_STARTED: "_started",
    ev.ALLOCATE_STARTED: "_allocate",
    ev.ATTACK_ATTEMPTED: "_attempted",
    ev.CHALLENGE_ATTACKED: "_attacked",
    ev.ROUND_SCORED: "_scored",
    ev.STAKES_SET: "_staked",
    ev.KEYSTONE_REVEALED: "_revealed",
    ev.MATCH_FAILED: "_failed",
    ev.MATCH_RECOVERED: "_recovered",
    ev.FINDING_FLAGGED: "_flagged",
    ev.GHOST_RUN: "_ghost_ran",
    ev.TERRAIN_DRAWN: "_terrain",
}


def snippet(source: str) -> list[str]:
    lines = [ln.rstrip() for ln in source.strip().splitlines() if ln.strip()]
    shown = [scrub(ln)[:SNIPPET_WIDTH] for ln in lines[:SNIPPET_LINES]]
    if len(lines) > SNIPPET_LINES:
        shown.append(f"… (+{len(lines) - SNIPPET_LINES} lines)")
    return shown


class MatchFold:
    """Base EventObserver: folds each event into a private copy of the match with
    ``domain.events.apply`` (the code the engine and replay use), then calls the subclass hook
    named in ``HOOKS`` (e.g. ``_built(state, event)``) when it defines one.
    """

    def __init__(self, match_id: str) -> None:
        self._match_id = match_id
        self._state: MatchState | None = None

    @property
    def state(self) -> MatchState | None:
        return self._state

    def __call__(self, event: Event) -> None:
        if self._state is None:
            self._state = ev.initial_state(self._match_id, event)
        ev.apply(self._state, event)
        hook = HOOKS.get(event.type)
        handler: Callable[[MatchState, Event], None] | None = getattr(self, hook, None) if hook else None
        if handler is not None:
            handler(self._state, event)


@dataclass(frozen=True)
class AttemptView:
    attacker: str
    author: str
    title: str
    try_no: int
    captured: bool
    award: int


def attempt_view(s: MatchState, e: Event) -> AttemptView:
    key = str(e.data["key"])
    cid, _, attacker = key.rpartition(":")
    n = len(s.attempts[key])
    ch = s.challenges[cid]
    assert ch.spec is not None, "only valid challenges are attacked"
    captured = s.attempts[key][-1].captured
    award = solve_award(ch.spec, ch.round, n, s.rules.scoring) if captured else 0
    return AttemptView(attacker, ch.author, scrub(ch.spec.title), n, captured, award)


def defense(s: MatchState, e: Event) -> tuple[Attack, Challenge, int] | None:
    """(attack, challenge, award) when the challenge held, else None."""
    atk = Attack.model_validate(e.data["attack"])
    ch = s.challenges[atk.challenge_id]
    assert ch.spec is not None, "only valid challenges are attacked"
    if atk.solved:
        return None
    return atk, ch, defense_award(ch.spec, ch.round, s.rules.scoring)


def verdict(s: MatchState, e: Event) -> tuple[Challenge, str, bool]:
    """(challenge, shown title, valid) plus the verdict text via ``verdict_text``."""
    v = Validation.model_validate(e.data["validation"])
    ch = s.challenges[v.challenge_id]
    title = scrub(ch.spec.title) if ch.spec else "forfeit"
    return ch, title, v.valid


def verdict_text(s: MatchState, e: Event) -> str:
    v = Validation.model_validate(e.data["validation"])
    ch = s.challenges[v.challenge_id]
    return "solvable ✓" if v.valid else f"rejected ✗ ({scrub(v.reason)}) -{s.rules.scoring.invalid_penalty * ch.round}"


def board(s: MatchState) -> str:
    final = s.final_scores()
    return "  ".join(f"{a.name} {final[a.name]}" for a in s.agents)


def handicap_text(h: Handicap) -> str:
    parts = [
        f"tries {h.attempts_delta:+d}" if h.attempts_delta else "",
        f"attack tokens x{h.token_budget_mult:g}" if h.token_budget_mult != 1 else "",
        f"clock x{h.bank_mult:g}" if h.bank_mult != 1 else "",
        f"score {h.score_offset:+d}" if h.score_offset else "",
    ]
    return ", ".join(x for x in parts if x)


def spend(s: MatchState, lifetime_usd: float, cap_usd: float) -> str:
    return f"${s.usage.usd:.4f} this match · ${lifetime_usd:.4f} of ${cap_usd:g} cap"


def _per(usd: float, n: int) -> str:
    return f"${usd / n:.4f}" if n else "—"


def agent_cost(s: MatchState, name: str) -> str:
    """One agent's tokens in/out (thinking share), dollars, and dollars per capture and per defense.

    Per capture: the agent's attack spend over its captures. Per defense: the rest of its spend (builds and
    repairs) over its challenges that held. Every call is counted, failed or not."""
    u = s.usage_by_agent.get(name) or Usage()
    mine = [a for a in s.attacks.values() if a.attacker == name]
    attack_usd = sum(a.usd_micros for a in mine) / 1_000_000
    captures = sum(a.solved for a in mine)
    defenses = sum(not a.solved and s.challenges[a.challenge_id].author == name for a in s.attacks.values())
    return (
        f"in {u.input_tokens:,} · out {u.output_tokens:,} (think {u.reasoning_tokens:,}) · ${u.usd:.4f} · "
        f"{_per(attack_usd, captures)}/capture · {_per(max(u.usd - attack_usd, 0.0), defenses)}/defense"
    )


def leader(s: MatchState) -> str | None:
    """Name of the agent ahead on the shared tiebreak (``series.decide``), or None on a full tie."""
    return decide(s, s.final_scores()) if len(s.agents) == 2 else None


def result(s: MatchState) -> str:
    top = leader(s)
    return f"draw: {board(s)}" if top is None else f"winner: {top} · {board(s)}"
