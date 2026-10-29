"""Ghost Codex, phase 1 (design-deep-layer.md layer 2): every live capture's winning payload is replayed, at $0,
against the opponent's later challenges. Pure: ghosts and their order come from folded state and the seed only.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from claude_duel.domain.models import Challenge, MatchState


@dataclass(frozen=True)
class Ghost:
    id: str  # the capture's attack key, "<challenge id>:<attacker>"
    attacker: str
    round: int
    payload: str


def ghosts_before(state: MatchState, round_no: int) -> list[Ghost]:
    """Live captures of earlier rounds (ghost captures never breed ghosts), sorted by id."""
    out = []
    for key, atk in state.attacks.items():
        ch = state.challenges.get(atk.challenge_id)
        if ch is None or ch.round >= round_no or not atk.solved or atk.ghost is not None:
            continue
        won = [a for a in state.attempts.get(key, []) if a.captured]
        if won:
            out.append(Ghost(id=key, attacker=atk.attacker, round=ch.round, payload=won[-1].payload))
    return sorted(out, key=lambda g: g.id)


def ghost_schedule(
    seed: str, round_no: int, ghosts: Iterable[Ghost], challenges: Iterable[Challenge], cap: int
) -> list[tuple[Ghost, Challenge]]:
    """(ghost, target) pairs: each ghost against every challenge of its victim's side this round, in an order that
    is a pure function of (seed, round, ghost ids, challenge ids), cut at ``cap``."""
    targets: Sequence[Challenge] = [c for c in challenges if c.round == round_no]
    pairs = [(g, c) for g in ghosts for c in targets if c.author != g.attacker]

    def slot(p: tuple[Ghost, Challenge]) -> bytes:
        return hashlib.sha256(f"{seed}:{round_no}:{p[0].id}>{p[1].id}:ghost".encode()).digest()

    order = sorted(pairs, key=slot)
    return order[:cap]


def ghost_award(full: int) -> int:
    """A ghost capture pays a quarter of the live capture, at least 1 (ADR 0006: discount only)."""
    return max(1, full // 4)
