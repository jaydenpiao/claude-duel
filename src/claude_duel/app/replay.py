"""Deterministic replay: fold the hash-chained event log and compare it with the snapshot (ADR 0004)."""

from __future__ import annotations

import platform
from dataclasses import dataclass, field

from claude_duel.domain import events as ev
from claude_duel.domain import ghosts, stakes
from claude_duel.domain.errors import HeatBoundError
from claude_duel.domain.models import Event, MatchState
from claude_duel.domain.scoring import add_scores, capture_points, heat_bound_of_state, heat_of_state, score_round

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
    warnings: list[str] = field(default_factory=list)


def replay(snapshot: MatchState, events: list[Event]) -> ReplayReport:
    """Fold events from scratch, rescore every round from folded facts, and diff against the snapshot.

    Only events the snapshot has seen are compared; a crash can leave the log ahead (``duel resume`` catches up).
    """
    events = [e for e in events if e.seq <= snapshot.last_seq]
    folded = ev.fold(snapshot.match_id, events)
    mismatches = [f"snapshot.{name}" for name in _COMPARED if getattr(folded, name) != getattr(snapshot, name)]
    recomputed, unverified = _rescore(folded, events, mismatches)
    # A round past the heat ceiling was not rescored; its recorded total is not claimed exact.
    if not unverified and recomputed != folded.scores:
        mismatches.append("total score")
    transcript = [f"#{e.seq:04d} {e.type} {e.data}" for e in events]
    return ReplayReport(not mismatches, recomputed, folded.scores, mismatches, transcript, _python_warning(events))


def _python_warning(events: list[Event]) -> list[str]:
    """Heat tokens come from ``ast``, whose node set can shift between Python minor versions: flag a mismatch."""
    created = next((e for e in events if e.type == ev.MATCH_CREATED), None)
    logged = str(created.data.get("python", "")) if created else ""
    here = platform.python_version()
    if not logged or logged.split(".")[:2] == here.split(".")[:2]:
        return []
    return [f"log written on Python {logged}, replayed on {here}: heat similarity may differ"]


def _rescore(folded: MatchState, events: list[Event], mismatches: list[str]) -> tuple[dict[str, int], bool]:
    """Rescore every scored round from folded facts, noting rounds whose recorded delta differs.

    Returns whether any round's heat was left unverified because the legacy comparison exceeds the work ceiling.
    That round is not rescored and is not reported as tampered.
    """
    names = [a.name for a in folded.agents]
    recomputed: dict[str, int] = {}
    unverified = False
    for event in (e for e in events if e.type == ev.ROUND_SCORED):
        r = int(str(event.data["round"]))
        points = capture_points(r, folded.challenges.values(), folded.validations, folded.attacks, folded.rules)
        # logs written before #199 carry no points; the delta check below still covers their score
        if "points" in event.data and points != event.data["points"]:
            mismatches.append(f"round {r} points tampered")
        try:
            delta = score_round(
                r,
                names,
                folded.challenges.values(),
                folded.validations,
                folded.attacks,
                folded.rules,
                stakes.keystone_targets(folded, r),
                folded.heat_refs,
                ghosts.round_hits(folded, r),
            )
        except HeatBoundError as exc:
            mismatches.append(f"round {r} heat unverified: {exc}")
            mismatches.append(f"round {r} score not checked")
            unverified = True
            continue
        if delta != event.data["delta"]:
            mismatches.append(f"round {r} score")
        if heat_of_state(folded, r) != event.data.get("heat", {}) or heat_bound_of_state(folded, r) != event.data.get(
            "heat_bound", []
        ):
            mismatches.append(f"round {r} heat tampered")
        recomputed = add_scores(recomputed, delta)
    return recomputed, unverified
