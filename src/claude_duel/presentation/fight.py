"""Fight replay: a self-contained HTML animation of a finished match (``duel fight``).

``fight_beats`` folds the event log (``MatchFold``) into ``Beat``s; ``fight_html`` embeds them in
``assets/fight.html``. Pure: the same log gives the same file byte for byte, and the page draws every frame
as a pure function of (beats, seed, frame). Only whitelisted fields leave the log: names, models, scrubbed
titles (40 chars), scores and spend (``common.usage_text``); never a flag, solution or source.
Design: docs/design/fight-animation.md.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from importlib import resources
from typing import Any

from claude_duel.domain.flags import scrub
from claude_duel.domain.models import Challenge, Event, MatchState, Usage
from claude_duel.presentation.common import MatchFold, attempt_view, defense, result, usage_text, verdict

TITLE_CHARS = 40
INTRO, BUILD, ATTACK, IMPACT, DEFEND, FIZZLE, ROUND_END, KO = (
    "intro", "build", "attack", "impact", "defend", "fizzle", "round_end", "ko",
)  # fmt: skip
STRIKE = 30  # windup 12 + projectile travel 18; the page times its effects inside each beat
HITSTOP = {IMPACT: 5, DEFEND: 4, KO: 6}  # freeze frames after contact; passed to the page in the payload
FRAMES = {  # beat length at 30fps (design §4): the one timeline both the page and the renderer use
    INTRO: 45, BUILD: 30, ATTACK: STRIKE + 12, IMPACT: STRIKE + 1 + HITSTOP[IMPACT] + 27,
    DEFEND: STRIKE + HITSTOP[DEFEND] + 20,
    FIZZLE: 20, ROUND_END: 30, KO: 60 + 30,
}  # fmt: skip
# Miss variants (design "Miss variants"): (id, beat kinds, weight). The page's VARIANTS table renders each id.
MISS_KINDS = (ATTACK, DEFEND)
MISS_VARIANTS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("graze", (ATTACK,), 3), ("parry", (ATTACK, DEFEND), 3), ("dodge", (ATTACK,), 2), ("shatter", (DEFEND,), 3),
    ("stumble", (ATTACK,), 2), ("feint", (ATTACK, DEFEND), 1), ("timeout", (DEFEND,), 3), ("whiff", (ATTACK,), 2),
)  # fmt: skip
SALT_MISS = 0x4D155
REASON_TAGS = {"attempts exhausted": "attempts", "token budget exhausted": "tokens"}


@dataclass(frozen=True)
class Side:
    tokens_in: int
    tokens_out: int
    usd: float
    hud: str  # common.usage_text of the cumulative usage at this beat


@dataclass(frozen=True)
class Beat:
    kind: str
    start: int  # first frame
    round: int
    actor: str  # "" is the arena itself
    target: str
    outcome: str  # the caption
    points: int  # awarded by this beat (capture or defense)
    score: dict[str, int]
    sides: dict[str, Side]
    pressure: float = 0.0  # 0..1, how desperate a miss is (tries or tokens used), never how close it was
    reason_tag: str = ""  # why a defense held: "attempts", "tokens" or ""
    variant: str = ""  # miss variant id (MISS_VARIANTS), picked by pick_variants


@dataclass(frozen=True)
class FightData:
    match_id: str
    players: list[tuple[str, str]]  # (name, model), left side first
    rounds: int
    seed: int  # uint32 from the match seed, else the log digest
    digest: str
    beats: list[Beat]
    frames: int
    hitstop: dict[str, int]


def _title(ch: Challenge) -> str:
    return scrub(ch.spec.title)[:TITLE_CHARS] if ch.spec else "forfeit"


def _side(u: Usage) -> Side:
    return Side(u.input_tokens, u.output_tokens, round(u.usd, 6), usage_text(u))


class _Beats(MatchFold):
    def __init__(self, match_id: str) -> None:
        super().__init__(match_id)
        self.beats: list[Beat] = []

    def _add(
        self, s: MatchState, kind: str, outcome: str, actor: str = "", target: str = "", points: int = 0, **miss: Any
    ) -> None:
        names = [a.name for a in s.agents]
        last = self.beats[-1] if self.beats else None
        self.beats.append(
            Beat(
                kind=kind,
                start=last.start + FRAMES[last.kind] if last else 0,
                round=s.round,
                actor=scrub(actor),
                target=scrub(target),
                outcome=scrub(outcome),
                points=points,
                score={scrub(n): s.scores.get(n, 0) for n in names},
                sides={scrub(n): _side(s.usage_by_agent.get(n) or Usage()) for n in names},
                **miss,
            )
        )

    def _created(self, s: MatchState, e: Event) -> None:
        self._add(s, INTRO, " vs ".join(a.name for a in s.agents))

    def _built(self, s: MatchState, e: Event) -> None:
        ch = Challenge.model_validate(e.data["challenge"])
        self._add(s, BUILD, f"{ch.author} forges '{_title(ch)}'", ch.author)

    def _validated(self, s: MatchState, e: Event) -> None:
        ch, _, valid = verdict(s, e)
        if not valid:
            self._add(s, FIZZLE, f"'{_title(ch)}' fizzles", ch.author)

    def _attempted(self, s: MatchState, e: Event) -> None:
        a = attempt_view(s, e)
        title = a.title[:TITLE_CHARS]
        if a.captured:
            self._add(s, IMPACT, f"{a.attacker} cracks '{title}' +{a.award}", a.attacker, a.author, a.award)
        else:
            p = _pressure(a.try_no / s.max_attempts(a.attacker))
            self._add(s, ATTACK, f"{a.attacker} misses '{title}' (try {a.try_no})", a.attacker, a.author, pressure=p)

    def _attacked(self, s: MatchState, e: Event) -> None:
        held = defense(s, e)
        if held is not None:
            atk, ch, award = held
            tag = REASON_TAGS.get(atk.reason, "")
            p = _pressure(1.0 if tag == "attempts" else atk.tokens_used / s.attack_token_budget(atk.attacker))
            held_by = f"'{_title(ch)}' holds +{award}"
            self._add(s, DEFEND, held_by, ch.author, atk.attacker, award, pressure=p, reason_tag=tag)

    def _scored(self, s: MatchState, e: Event) -> None:
        if s.round >= s.rules.rounds:
            self._add(s, KO, result(s))
        else:
            self._add(s, ROUND_END, f"round {e.data['round']} scored")

    def _failed(self, s: MatchState, e: Event) -> None:
        self._add(s, KO, f"match failed: {e.data.get('reason', '')}")


def _pressure(p: float) -> float:
    return min(1.0, max(0.0, round(p, 2)))


def mulberry32(seed: int) -> Callable[[], float]:
    """The page's ``rng``: same uint32 math, so a seed gives the same stream in both languages."""
    a = seed & 0xFFFFFFFF

    def imul(x: int, y: int) -> int:
        return (x * y) & 0xFFFFFFFF

    def nxt() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = imul(a ^ (a >> 15), 1 | a)
        t = ((t + imul(t ^ (t >> 7), 61 | t)) & 0xFFFFFFFF) ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return nxt


def miss_pool(b: Beat, prev: str) -> list[tuple[str, int]]:
    """(variant, weight) a miss beat may show: its kind's variants minus the previous pick."""
    pool = []
    for vid, kinds, weight in MISS_VARIANTS:
        if b.kind not in kinds or vid == prev or (vid == "timeout" and b.reason_tag != "tokens"):
            continue
        last_try = vid == "stumble" and b.kind == ATTACK and b.pressure == 1.0
        pool.append((vid, weight * 2 if last_try else weight))
    return pool


def pick_variants(beats: list[Beat], seed: int) -> list[Beat]:
    """Seeded, weighted miss variants with no immediate repeat (``prev`` spans the whole match)."""
    out, prev = [], ""
    for i, b in enumerate(beats):
        if b.kind in MISS_KINDS:
            pool = miss_pool(b, prev)
            total = sum(w for _, w in pool)
            x = min(total - 1, int(mulberry32(seed ^ i ^ SALT_MISS)() * total))
            prev = next(vid for vid, cum in _cumulative(pool) if x < cum)
        out.append(replace(b, variant=prev) if b.kind in MISS_KINDS else b)
    return out


def _cumulative(pool: list[tuple[str, int]]) -> list[tuple[str, int]]:
    total, out = 0, []
    for vid, w in pool:
        total += w
        out.append((vid, total))
    return out


def fight_beats(state: MatchState, events: list[Event]) -> list[Beat]:
    fold = _Beats(state.match_id)
    for e in events:
        if e.seq <= state.last_seq:
            fold(e)
    return fold.beats


def fight_data(state: MatchState, events: list[Event], digest: str) -> FightData:
    seed = int(hashlib.sha256((state.seed or digest).encode()).hexdigest()[:8], 16)
    beats = pick_variants(fight_beats(state, events), seed)
    return FightData(
        match_id=scrub(state.match_id),
        players=[(scrub(a.name), scrub(a.model or "default")) for a in state.agents],
        rounds=state.rules.rounds,
        seed=seed,
        digest=digest,
        beats=beats,
        frames=beats[-1].start + FRAMES[beats[-1].kind] if beats else 1,
        hitstop=HITSTOP,
    )


def fight_html(d: FightData) -> str:
    """Pure: the page. JSON is sorted and ``<`` is escaped, so no text can close the script tag."""
    payload = json.dumps(asdict(d), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    template = resources.files("claude_duel.presentation").joinpath("assets/fight.html").read_text(encoding="utf-8")
    return template.replace("__BEATS__", payload.replace("<", "\\u003c"))
