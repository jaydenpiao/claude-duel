"""Offline control variate for duplicate matches (design-no-fluke control variates, AIVAT-style), $0 and reported only.

Each board is replayed on its own seed and pinned rules by two equal scripted bots. Their board difference is the
luck the seed alone deals (who builds what, which draws land); subtracting it from the real difference leaves a
lower-variance estimate of skill. It never changes a recorded result: the ladder still decides on raw boards.
"""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.app.container import build_duplicate_log
from claude_duel.app.harness import scripted_half
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain import match_stats as ms
from claude_duel.domain.models import AgentSpec

EVEN_PCT = 50  # both baseline bots capture with this chance per try


@dataclass(frozen=True)
class Adjusted:
    duplicate_id: str
    raw: list[int]  # recorded board differences (void boards excluded)
    baseline: list[int]  # equal scripted bots on the same seeds
    adjusted: list[int]  # raw - baseline
    p_value: float  # sign test on adjusted boards
    ci95: tuple[float, float]  # seeded bootstrap of the adjusted mean


def adjust(settings: Settings, duplicate_id: str) -> Adjusted:
    """Baseline every scored board of ``duplicate_id`` with equal scripted bots and report the adjusted margin.
    Baseline matches live under ``<store>/control-variate`` (resumable, ids ``cv-<board half>``)."""
    state = dp.fold(duplicate_id, build_duplicate_log(settings).read(duplicate_id))
    bots = tuple(AgentSpec(name=a.name) for a in state.agents)
    pcts = dict.fromkeys((a.name for a in bots), EVEN_PCT)
    scratch = settings.model_copy(
        update={"store_dir": settings.store_dir / "control-variate", "sandbox": "subprocess", "rules": state.rules}
    )
    raw, base = [], []
    for b in (b for b in state.boards if not b.void):
        halves = tuple(
            scripted_half(
                scratch.model_copy(update={"seed": b.seed}),
                f"cv-{state.half_id(b.board_no, h, b.replays)}",
                dp.half_agents((bots[0], bots[1]), h),
                pcts,
            )
            for h in (0, 1)
        )
        raw.append(b.diff)
        base.append(dp.board_of(state, b.board_no, b.seed, (halves[0], halves[1]), b.replays).diff)
    adjusted = [r - x for r, x in zip(raw, base, strict=True)]
    rec = ms.record(adjusted)
    return Adjusted(
        duplicate_id,
        raw,
        base,
        adjusted,
        ms.sign_test_p(rec.wins, rec.losses),
        ms.bootstrap_ci(adjusted, f"{state.seed}:cv"),
    )
