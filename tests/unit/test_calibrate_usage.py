"""scripts/calibrate_usage.py: per-model mean tokens and calls per challenge from match logs (no calls, files only)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("calibrate_usage", ROOT / "scripts" / "calibrate_usage.py")
assert _spec is not None
assert _spec.loader is not None
calibrate_usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate_usage)


def _log(path: Path, cap: int, calls: list[tuple[int, int, int]], *, finished: bool = True) -> None:
    rules = {"rounds": 1, "challenges_per_round": 1}
    events: list[dict[str, object]] = [
        {
            "type": "match_created",
            "data": {"rules": rules, "agents": [{"name": "a", "model": "m", "max_output_tokens": cap}]},
        }
    ]
    events += [
        {
            "type": "call_charged",
            "data": {"agent": "a", "model": "m", "input_tokens": i, "output_tokens": o, "elapsed_ms": ms},
        }
        for i, o, ms in calls
    ]
    if finished:
        events.append({"type": "round_scored", "data": {}})
    path.write_text("\n".join(json.dumps({"event": e}) for e in events) + "\n")


def test_means_and_calls_per_challenge_over_two_matches(tmp_path: Path) -> None:
    _log(tmp_path / "x.events.jsonl", 8000, [(1000, 2000, 1000), (3000, 4000, 3000)])
    _log(tmp_path / "y.events.jsonl", 8000, [(2000, 3000, 2000)])
    row = calibrate_usage.calibrate(tmp_path, 8000)["m"]
    assert (row["calls"], row["input_tokens"], row["output_tokens"]) == (3, 2000, 3000)
    assert row["calls_per_challenge"] == 1.5  # mean of 2 and 1 calls over one challenge


def test_calls_under_the_cap_floor_are_ignored(tmp_path: Path) -> None:
    _log(tmp_path / "x.events.jsonl", 1000, [(1000, 2000, 1000)])
    assert calibrate_usage.calibrate(tmp_path, 8000) == {}
