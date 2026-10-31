"""app/export.py: whitelisted columns, digest manifest, secret scan before writing."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from claude_duel.app.export import FIELDS, build, scan, write
from claude_duel.domain.errors import FlagLeakError
from claude_duel.domain.flags import derive_flag
from claude_duel.ports import Row

HEAD = "a" * 64


class Views:
    def __init__(self, data: dict[str, list[Row]]) -> None:
        self.data = data

    def export(self) -> dict[str, list[Row]]:
        return self.data


def _match(**extra: object) -> Row:
    return {
        "match_id": "m1",
        "head_hash": HEAD,
        "usd": Decimal("0.5"),
        "first_synced_at": dt.datetime(2026, 1, 1),
    } | extra


def test_only_whitelisted_columns_and_manifest(tmp_path: Path) -> None:
    out = write(Views({"matches": [_match(rules={"secret": 1})]}), tmp_path / "x.json")
    data = json.loads((tmp_path / "x.json").read_text())
    row = data["tables"]["matches"][0]
    assert set(row) == set(FIELDS["matches"])
    assert (row["usd"], row["first_synced_at"]) == (0.5, "2026-01-01T00:00:00")
    assert set(data["tables"]) == set(FIELDS)
    assert out["manifest"] == {"heads": [f"m1 {HEAD}"], "digest": hashlib.sha256(f"m1 {HEAD}".encode()).hexdigest()}


@pytest.mark.parametrize(
    "bad",
    [derive_flag("k", "c"), "sk-abcdefghijklmnop", {"note": "api_key=xyz"}, ["ok", "f" * 40], {"b" * 40: 1}],
)
def test_scan_blocks_secret_shaped_values(bad: object, tmp_path: Path) -> None:
    with pytest.raises(FlagLeakError):
        scan({"matches": [{"winner": bad}]})
    with pytest.raises(FlagLeakError):
        write(Views({"matches": [_match(winner=bad)]}), tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()


def test_head_hash_is_public() -> None:
    scan({"matches": [{"head_hash": HEAD, "winner": "alice", "scores": {"alice": 3}}]})
    assert build(Views({}))["tables"] == {v: [] for v in FIELDS}
