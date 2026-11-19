"""Shareable replay card: a static SVG summary of a finished match for posting (``duel card``).

Pure: built from the saved state, the replay report and the event log. Every piece of text goes
through ``scrub`` and is XML-escaped; the match secret and flags are never read into the card.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from xml.sax.saxutils import escape

from claude_duel.app.replay import ReplayReport
from claude_duel.domain import events as ev
from claude_duel.domain.flags import scrub
from claude_duel.domain.models import Event, MatchState
from claude_duel.presentation.common import agent_cost, result, who

WIDTH, HEIGHT = 800, 420
COLORS = ("#22d3ee", "#e879f9")


def log_digest(events: list[Event]) -> str:
    """Short fingerprint of the replayed event log, so a posted card can be checked against `duel replay`."""
    h = hashlib.sha256()
    for e in events:
        h.update(e.model_dump_json().encode())
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class CardData:
    match_id: str
    players: list[tuple[str, str, int]]  # (label, model, score)
    costs: list[str]  # per player, common.agent_cost
    rounds: str
    headline: str
    spend: str
    verified: bool
    digest: str
    mismatches: tuple[str, ...] = ()


def card_data(state: MatchState, report: ReplayReport, events: list[Event]) -> CardData:
    players = [(who(state, a.name), a.model or "default", state.scores.get(a.name, 0)) for a in state.agents]
    return CardData(
        match_id=state.match_id,
        players=players,
        costs=[agent_cost(state, a.name) for a in state.agents],
        rounds=f"{state.round}/{state.rules.rounds} rounds · {state.stage.value}",
        headline=result(state),
        spend=(
            f"${state.usage.usd:.4f} · {state.usage.calls} model calls · "
            f"in {state.usage.input_tokens:,} · out {state.usage.output_tokens:,} tokens" + _refusal_label(events)
        ),
        verified=report.consistent,
        digest=log_digest(events),
        mismatches=tuple(report.mismatches),
    )


def _refusal_label(events: list[Event]) -> str:
    """A spend-line suffix when the log recorded a refusal; empty for older logs and matches with none."""
    n = sum(1 for e in events if e.type == ev.CALL_CHARGED and e.data.get("refused"))
    if not n:
        return ""
    return f" · {n} model refusal" + ("s" if n != 1 else "")


def _t(x: int, y: int, text: str, size: int = 20, fill: str = "#e5e7eb", weight: str = "normal") -> str:
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-weight="{weight}">{escape(scrub(text))}</text>'


def _bars(d: CardData) -> list[str]:
    top = max([s for _, _, s in d.players] + [1])
    out = []
    for i, (name, _, score) in enumerate(d.players):
        y = 150 + i * 70
        w = round(500 * max(score, 0) / top)
        out += [
            _t(40, y, name, 18, COLORS[i % 2], "bold"),
            f'<rect x="40" y="{y + 10}" width="500" height="22" rx="4" fill="#1f2937"/>',
            f'<rect x="40" y="{y + 10}" width="{w}" height="22" rx="4" fill="{COLORS[i % 2]}"/>',
            _t(560, y + 28, str(score), 22, "#f9fafb", "bold"),
            _t(40, y + 50, d.costs[i] if i < len(d.costs) else "", 13, "#9ca3af"),
        ]
    return out


def _verdict(d: CardData) -> str:
    """Verified, a real mismatch, or heat that a legacy log cannot replay (not labeled as tampering)."""
    if d.verified:
        return "replay verified ✓"
    legacy = ("heat legacy-unreplayable", "score not checked")
    if d.mismatches and all(any(part in item for part in legacy) for item in d.mismatches):
        return "replay heat legacy-unreplayable"
    return "replay MISMATCH ✗"


def card_svg(d: CardData) -> str:
    """Pure: the card as an SVG document."""
    names = " vs ".join(f"{n}" for n, _, _ in d.players)
    verdict = _verdict(d) + f" · log {d.digest}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        'font-family="ui-monospace, Menlo, monospace">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="16" fill="#111827"/>',
        _t(40, 60, f"claude duel · {names}", 28, "#f9fafb", "bold"),
        _t(40, 95, f"match {d.match_id} · {d.rounds}", 16, "#9ca3af"),
        *_bars(d),
        _t(40, 320, d.headline, 22, "#fde047", "bold"),
        _t(40, 355, d.spend, 16, "#9ca3af"),
        _t(40, 390, verdict, 16, "#86efac" if d.verified else "#fca5a5"),
        "</svg>",
    ]
    return "\n".join(parts) + "\n"
