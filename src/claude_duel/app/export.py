"""Static public export (ADR 0012): whitelisted view columns, a digest manifest of log heads, a secret scan.

The file is what a cached public page serves; the database itself is never exposed. Every string is checked with
``flags.scrub`` before anything is written; one hit aborts the export.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from claude_duel.domain.errors import FlagLeakError
from claude_duel.domain.flags import scrub
from claude_duel.ports import PublicSource, Row

FORMAT = "llm-duels-export/1"
FIELDS: dict[str, tuple[str, ...]] = {
    "matches": (
        "match_id",
        "head_hash",
        "event_count",
        "stage",
        "round",
        "mode",
        "challenge_form",
        "framing",
        "first_synced_at",
        "winner",
        "scores",
        "calls",
        "tokens",
        "usd",
    ),
    "match_agents": ("match_id", "agent_name", "entrant_id"),
    "leaderboard": (
        "entrant_id",
        "games",
        "elo",
        "bt",
        "bt_lo",
        "bt_hi",
        "elo_order",
        "elo_k",
        "through_match",
        "computed_at",
    ),
    "rating_history": ("sync_run_id", "computed_at", "entrant_id", "games", "elo", "bt", "bt_lo", "bt_hi"),
    "model_daily": ("day", "entrant_id", "attacks", "captures", "defended_challenges", "defenses"),
    "cost_daily": ("day", "model", "usd", "tokens", "calls", "matches"),
}
_HASHES = frozenset({"head_hash"})  # 64-hex digests are public by design; scrub would flag them


def _plain(v: object) -> object:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    return v


def build(source: PublicSource) -> dict[str, object]:
    data = source.export()
    tables = {view: [{c: _plain(r.get(c)) for c in cols} for r in data.get(view, [])] for view, cols in FIELDS.items()}
    heads = sorted(f"{r['match_id']} {r['head_hash']}" for r in tables["matches"])
    manifest = {"heads": heads, "digest": hashlib.sha256("\n".join(heads).encode()).hexdigest()}
    out: dict[str, object] = {"format": FORMAT, "manifest": manifest, "tables": tables}
    scan(tables)
    return out


def scan(tables: dict[str, list[Row]]) -> None:
    """Raise ``FlagLeakError`` if any exported string looks like a flag, key or secret."""
    for view, rows in tables.items():
        for row in rows:
            for col, value in row.items():
                if col not in _HASHES and _dirty(value):
                    raise FlagLeakError(f"secret-shaped value in export {view}.{col}")


def _dirty(value: object) -> bool:
    if isinstance(value, dict):
        return any(_dirty(k) or _dirty(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_dirty(v) for v in value)
    return isinstance(value, str) and scrub(value) != value


def write(source: PublicSource, path: Path) -> dict[str, object]:
    out = build(source)
    path.write_text(json.dumps(out, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return out
