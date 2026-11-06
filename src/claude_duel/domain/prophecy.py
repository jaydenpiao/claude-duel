"""Prophecy (design-deep-layer.md, Layer 1, menu tier): sealed forecasts about the opponent's next round.

Pure. A prophecy is up to 3 atoms (all must hold, each may be negated) over the opponent's logged round
artifacts; it is committed as ``sha256(canonical_json + salt)`` and settled after that round is scored.
Hit: +stake, miss: -stake, never below 0 points; a vacuous template (corpus hit rate > 95% or < 5%) pays 0;
the per-round payout is capped and the match swing never exceeds ``pool``. Never touches a capture (ADR 0006).
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, ValidationError

from claude_duel.domain.models import _Frozen
from claude_duel.domain.stakes import LedgerRule

Kind = Literal["ghost_kills_ge", "keystone_is", "uses_construct", "capture_count_le", "max_stake_ge"]
Selector = Literal["longest", "shortest", "first"]
MAX_ATOMS = 3
VACUITY = (0.05, 0.95)


class Atom(_Frozen):
    kind: Kind
    n: int = Field(default=0, ge=0)
    selector: Selector | None = None
    tag: str | None = None
    negate: bool = False


class Prophecy(_Frozen):
    template: str
    atoms: tuple[Atom, ...] = Field(min_length=1, max_length=MAX_ATOMS)
    stake: int = Field(ge=1, le=3)


class ChallengeFacts(_Frozen):
    cid: str
    size: int  # source bytes
    tag: str  # memory.technique


class RoundArtifacts(_Frozen):
    """What the target authored and did in the settled round, all from the event log."""

    challenges: tuple[ChallengeFacts, ...]  # in authored order
    keystone: str | None = None
    captures: int = 0  # captures the target made
    ghost_kills: int = 0  # the target's challenges killed by ghosts
    stakes: tuple[int, ...] = ()  # the target's attack stakes


def _atom(a: Atom, art: RoundArtifacts) -> bool:
    match a.kind:
        case "ghost_kills_ge":
            hit = art.ghost_kills >= a.n
        case "capture_count_le":
            hit = art.captures <= a.n
        case "max_stake_ge":
            hit = max(art.stakes, default=0) >= a.n
        case "uses_construct":
            hit = any(c.tag == a.tag for c in art.challenges)
        case "keystone_is":
            hit = art.keystone is not None and art.keystone == _select(a.selector, art.challenges)
    return hit != a.negate


def _select(sel: Selector | None, cs: tuple[ChallengeFacts, ...]) -> str | None:
    if not cs:
        return None
    if sel == "first":
        return cs[0].cid
    key = (lambda c: (c.size, c.cid)) if sel == "shortest" else (lambda c: (-c.size, c.cid))
    return min(cs, key=key).cid


def holds(p: Prophecy, art: RoundArtifacts) -> bool:
    return all(_atom(a, art) for a in p.atoms)


def canonical(p: Prophecy) -> str:
    return json.dumps(p.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def commit(p: Prophecy, salt: str) -> str:
    return hashlib.sha256((canonical(p) + salt).encode()).hexdigest()


def verify(p: Prophecy, salt: str, sealed: str) -> bool:
    return commit(p, salt) == sealed


def _a(kind: Kind, **kw: object) -> Atom:
    return Atom.model_validate({"kind": kind, **kw})


TEMPLATES: dict[str, tuple[Atom, ...]] = {
    "keystone_longest": (_a("keystone_is", selector="longest"),),
    "keystone_shortest": (_a("keystone_is", selector="shortest"),),
    "keystone_first": (_a("keystone_is", selector="first"),),
    "uses_hash": (_a("uses_construct", tag="hash-check"),),
    "uses_vm": (_a("uses_construct", tag="vm"),),
    "uses_checksum": (_a("uses_construct", tag="checksum"),),
    "no_hash": (_a("uses_construct", tag="hash-check", negate=True),),
    "ghost_kill": (_a("ghost_kills_ge", n=1),),
    "no_captures": (_a("capture_count_le", n=0),),
    "one_capture_max": (_a("capture_count_le", n=1),),
    "all_in": (_a("max_stake_ge", n=LedgerRule().attack_tokens // 2),),
    "longest_keystone_no_ghost": (_a("keystone_is", selector="longest"), _a("ghost_kills_ge", n=1, negate=True)),
}


def pick(raw: str) -> list[Prophecy] | None:
    """Parse a menu reply ``{"prophecies": [{"template": ..., "stake": 1-3}]}`` (max 2); None -> skipped."""
    try:
        items = json.loads(raw)["prophecies"]
        if not isinstance(items, list) or not 1 <= len(items) <= 2:
            return None
        return [Prophecy(template=i["template"], atoms=TEMPLATES[i["template"]], stake=i["stake"]) for i in items]
    except (ValueError, KeyError, TypeError, ValidationError):
        return None


def vacuous(rate: float | None) -> bool:
    return rate is not None and not VACUITY[0] <= rate <= VACUITY[1]


def settle(
    prophecies: list[Prophecy],
    art: RoundArtifacts,
    *,
    points: int,
    swing: int,
    pool: int,
    round_cap: int,
    rates: dict[str, float],
) -> tuple[int, list[bool]]:
    """(delta, hits) for one prophet's round. ``points``/``swing`` are its prophecy totals so far."""
    hits = [holds(p, art) for p in prophecies]
    delta = sum(
        0 if vacuous(rates.get(p.template)) else (p.stake if h else -p.stake)
        for p, h in zip(prophecies, hits, strict=True)
    )
    delta = max(-round_cap, min(round_cap, delta), -points)
    room = max(0, pool - swing)
    delta = max(-room, min(room, delta))
    return delta, hits
