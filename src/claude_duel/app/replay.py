"""Deterministic replay: recompute scores from persisted facts and compare to the log."""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.domain.models import MatchState
from claude_duel.domain.scoring import add_scores, score_round


@dataclass(frozen=True)
class ReplayReport:
    consistent: bool
    recomputed: dict[str, int]
    recorded: dict[str, int]
    transcript: list[str]


def replay(state: MatchState) -> ReplayReport:
    totals: dict[str, int] = {}
    rounds = sorted({c.round for c in state.challenges.values()})
    names = [a.name for a in state.agents]
    for r in rounds:
        totals = add_scores(
            totals, score_round(r, names, state.challenges.values(), state.validations, state.attacks, state.rules)
        )
    recorded: dict[str, int] = {}
    for ev in state.events:
        if ev.type == "round_scored":
            raw = ev.data.get("totals")
            if isinstance(raw, dict):
                recorded = {str(k): int(v) for k, v in raw.items()}
    transcript = [f"#{e.seq:04d} {e.type} {e.data}" for e in state.events]
    return ReplayReport(consistent=totals == recorded, recomputed=totals, recorded=recorded, transcript=transcript)
