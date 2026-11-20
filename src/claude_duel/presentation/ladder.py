"""Dry-run projection and step table for `duel ladder` (plain text or JSON; pure)."""

from __future__ import annotations

import math
from collections import Counter
from typing import TYPE_CHECKING, Any, TextIO, cast

from claude_duel.domain import events as ev
from claude_duel.domain.ladder import LadderState
from claude_duel.domain.models import Event

if TYPE_CHECKING:
    from claude_duel.app.ladder import Comparison, Projection

_WINNER = {"first": "a", "second": "b"}


def projection_lines(proj: Projection, ladder_max_usd: float) -> list[str]:
    lines = [f"{'side a':<20}{'side b':<20}{'worst $':>10}{'typical $':>11}{'calibrated $':>14}"]
    lines += [
        f"{r.a:<20}{r.b:<20}{r.worst_usd:>10.2f}{r.typical_usd:>11.2f}{r.calibrated_usd:>14.2f}" for r in proj.rows
    ]
    lines.append(f"worst-case ladder ${proj.total_usd:.2f} / ladder_max_usd ${ladder_max_usd:.2f}")
    dearest = max((r.typical_usd for r in proj.rows), default=0.0)
    lines.append(
        f"typical ladder ${proj.typical_usd():.2f} (dearest match ${dearest:.2f}): expected, no retries or replays,"
        f" assumed reply sizes; caps are checked against the {proj.basis} case"
    )
    lines.append(f"calibrated ladder ${proj.calibrated_usd():.2f}: each model at its measured mean tokens per call")
    if proj.basis in ("typical", "calibrated"):
        lines.append("hard stop: no call is made whose worst case could pass match_max_usd or ladder_max_usd")
    lines += [f"refused: {r}" for r in proj.refusals] or ["dry run ok: within every cap"]
    return lines


def projection_view(proj: Projection, ladder_max_usd: float) -> dict[str, object]:
    return {
        "rows": [
            {
                "a": r.a,
                "b": r.b,
                "worst_usd": round(r.worst_usd, 4),
                "typical_usd": round(r.typical_usd, 4),
                "calibrated_usd": _usd(r.calibrated_usd),
            }
            for r in proj.rows
        ],
        "total_usd": round(proj.total_usd, 4),
        "typical_usd": round(proj.typical_usd(), 4),
        "calibrated_usd": _usd(proj.calibrated_usd()),
        "ladder_max_usd": ladder_max_usd,
        "basis": proj.basis,
        "refusals": proj.refusals,
    }


def _usd(value: float) -> float | None:
    """Rounded dollars; ``None`` for a model with no calibrated token use (shown as nan in text)."""
    return None if math.isnan(value) else round(value, 4)


def ladder_view(state: LadderState, verified: bool, mismatches: list[str]) -> dict[str, object]:
    return {
        "ladder_id": state.ladder_id,
        "digest": state.digest,
        "steps": [s.model_dump() for s in state.steps],
        "spent_usd": round(state.spent_usd(), 4),
        "capped": state.capped,
        "done": state.done(),
        "verified": verified,
        "mismatches": mismatches,
    }


def ladder_lines(state: LadderState, verified: bool, mismatches: list[str]) -> list[str]:
    lines = [f"ladder {state.ladder_id} (plan {state.digest[:12]})"]
    for s in state.steps:
        side = _WINNER.get(s.result)
        tag = f"winner {s.a if side == 'a' else s.b}" if side else s.result
        lines.append(f"step {s.step_no}: {s.a} vs {s.b} · {tag} · ${s.usd:.4f} · {s.duplicate_id}")
    status = "CAPPED" if state.capped else "finished" if state.done() else "unfinished (run again to resume)"
    lines.append(f"{status} · ${state.spent_usd():.4f}")
    lines.append("verified ✓" if verified else "verified ✗ " + ", ".join(mismatches))
    return lines


def comparison_lines(cmp: Comparison) -> list[str]:
    lines = [f"{'side a':<20}{'side b':<20}{'run 1':<8}{'run 2':<8}verdict"]
    lines += [f"{m.a:<20}{m.b:<20}{m.results[0]:<8}{m.results[1]:<8}{m.verdict}" for m in cmp.matchups]
    lines.append(f"final ordering: {cmp.ordering}")
    return lines


def comparison_view(cmp: Comparison) -> dict[str, object]:
    return {
        "matchups": [{"a": m.a, "b": m.b, "results": list(m.results), "verdict": m.verdict} for m in cmp.matchups],
        "ordering": cmp.ordering,
    }


class LadderTicker:
    """EventObserver for `duel ladder --watch`: one line per half, build, attack and problem, plus a running spend.
    It prints ids, names, outcomes and dollars only (never payloads, prompts, framing text or flags)."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self.halves = 0
        self.usd = 0.0
        self.counts: Counter[str] = Counter()

    def _say(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()

    def __call__(self, e: Event) -> None:
        d = cast(dict[str, Any], e.data)
        if e.type == ev.MATCH_CREATED:
            self.halves += 1
            names = " vs ".join(str(a["name"]) for a in d["agents"])
            self._say(f"half {self.halves}: {names}")
        elif e.type == ev.CHALLENGE_BUILT:
            ch = cast(dict[str, Any], d["challenge"])
            what = "built" if ch.get("spec") else f"forfeit ({ch.get('forfeit_reason')})"
            self._say(f"  {ch['author']} {what} {ch['id']}")
        elif e.type == ev.CHALLENGE_ATTACKED:
            a = cast(dict[str, Any], d["attack"])
            outcome = "captured" if a["solved"] else "held"
            self._say(f"  {a['attacker']} vs {a['challenge_id']}: {outcome} in {a['attempts']} tries")
        elif e.type == ev.CALL_CHARGED:
            self.usd += float(d["usd"])
            for flag in ("truncated", "refused"):
                if d.get(flag):
                    self.counts[flag] += 1
                    self._say(f"  {d['agent']} {d['purpose']} reply {flag}")
        elif e.type in (ev.BUILD_REJECTED, ev.CHALLENGE_TIMEOUT, ev.MATCH_FAILED, ev.MATCH_CAPPED):
            self.counts[e.type] += 1
            self._say(f"  {e.type.replace('_', ' ')}")
        elif e.type == ev.STAGE_CHANGED and d.get("to") == "complete":
            self._say(f"  half done · ${self.usd:.4f} so far")
