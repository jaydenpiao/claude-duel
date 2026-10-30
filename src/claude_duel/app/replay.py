"""Deterministic replay: fold the hash-chained event log and compare it with the snapshot (ADR 0004)."""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.domain import events as ev
from claude_duel.domain import ghosts, stakes
from claude_duel.domain.models import Event, MatchState
from claude_duel.domain.scoring import add_scores, capture_points, heat_of_state, score_round

_COMPARED = (
    "stage",
    "round",
    "challenges",
    "validations",
    "attacks",
    "attempts",
    "scores",
    "usage",
    "usage_by_agent",
    "notes",
)


@dataclass(frozen=True)
class ReplayReport:
    consistent: bool
    recomputed: dict[str, int]
    recorded: dict[str, int]
    mismatches: list[str]
    transcript: list[str]


def replay(snapshot: MatchState, events: list[Event]) -> ReplayReport:
    """Fold events from scratch, rescore every round from folded facts, and diff against the snapshot.

    Only events the snapshot has seen are compared; a crash can leave the log ahead (``duel resume`` catches up).
    """
    events = [e for e in events if e.seq <= snapshot.last_seq]
    folded = ev.fold(snapshot.match_id, events)
    mismatches = [f"snapshot.{name}" for name in _COMPARED if getattr(folded, name) != getattr(snapshot, name)]
    recomputed = _rescore(folded, events, mismatches)
    if recomputed != folded.scores:
        mismatches.append("total score")
    transcript = [f"#{e.seq:04d} {e.type} {e.data}" for e in events]
    return ReplayReport(not mismatches, recomputed, folded.scores, mismatches, transcript)


def _rescore(folded: MatchState, events: list[Event], mismatches: list[str]) -> dict[str, int]:
    """Rescore every scored round from folded facts, noting rounds whose recorded delta differs."""
    names = [a.name for a in folded.agents]
    recomputed: dict[str, int] = {}
    for event in (e for e in events if e.type == ev.ROUND_SCORED):
        r = int(str(event.data["round"]))
        keystones = stakes.keystone_targets(folded, r)
        delta = score_round(
            r,
            names,
            folded.challenges.values(),
            folded.validations,
            folded.attacks,
            folded.rules,
            keystones,
            ghosts.round_hits(folded, r),
        )
        if delta != event.data["delta"]:
            mismatches.append(f"round {r} score")
        points = capture_points(r, folded.challenges.values(), folded.validations, folded.attacks, folded.rules)
        if points != event.data.get("points", {}):
            mismatches.append(f"round {r} points tampered")
        if heat_of_state(folded, r) != event.data.get("heat", {}):
            mismatches.append(f"round {r} heat tampered")
        recomputed = add_scores(recomputed, delta)
    return recomputed
