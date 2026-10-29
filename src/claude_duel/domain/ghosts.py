"""Ghost Codex, phase 1 (design-deep-layer.md layer 2): every live capture's winning payload is replayed, at $0,
against the opponent's later challenges. Pure: ghosts and their order come from folded state and the seed only.
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from claude_duel.domain.models import Challenge, MatchState

_MATCH_SUFFIX = re.compile(r"-[0-9a-f]{8}(?=$|:)")


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
    is a pure function of (seed, round, ghost ids, challenge ids), cut at ``cap``. The cap counts planned pairs,
    not runs: a pair whose target an earlier pair already hit is skipped by the arena but still used its slot."""
    targets: Sequence[Challenge] = [c for c in challenges if c.round == round_no]
    pairs = [(g, c) for g in ghosts for c in targets if c.author != g.attacker]

    def slot(p: tuple[Ghost, Challenge]) -> tuple[bytes, str, str]:
        key = f"{seed}:{round_no}:{_stable(p[0].id)}>{_stable(p[1].id)}:ghost"
        return hashlib.sha256(key.encode()).digest(), p[0].id, p[1].id

    order = sorted(pairs, key=slot)
    return order[:cap]


def _stable(cid: str) -> str:
    """An id without its per-match suffix ("r1-alice-0-1a2b3c4d" -> "r1-alice-0"), so the order is the same for the
    same seed under any match id (the seed, not the match, decides the schedule)."""
    return _MATCH_SUFFIX.sub("", cid)


def ghost_award(full: int) -> int:
    """A ghost capture pays a quarter of the live capture, at least 1 (ADR 0006: discount only)."""
    return max(1, full // 4)


def run_key(round_no: int, ghost_id: str, target: str) -> str:
    """``MatchState.ghost_runs`` key; ghost ids are "<challenge id>:<attacker>", targets are challenge ids."""
    return f"{round_no}:{ghost_id}>{target}"


def round_hits(state: MatchState, round_no: int) -> dict[str, str]:
    """challenge id -> the ghost that captured it in ``round_no``; empty unless the deep ghosts layer is on.

    The scorer accepts a ghost capture only if it matches this (the log's ``ghost_run`` hits), so a forged
    ``Attack.ghost`` is tampering."""
    if state.rules.deep is None or not state.rules.deep.ghosts:
        return {}
    hits: dict[str, str] = {}
    for key, hit in sorted(state.ghost_runs.items()):
        r, rest = key.split(":", 1)
        gid, target = rest.rsplit(">", 1)  # challenge ids never contain ">"
        if hit and int(r) == round_no:
            hits.setdefault(target, gid)
    return hits
