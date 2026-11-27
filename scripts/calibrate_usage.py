"""Regenerate ``claude_duel.app.calibration.MEASURED`` from real match logs ($0: reads files, makes no calls).

Usage: ``python scripts/calibrate_usage.py DIR [--min-cap 8000]``. DIR holds ``*.events.jsonl`` match logs. Only calls
whose agent ran with an output cap of at least ``--min-cap`` count, so the table reflects the arena's real caps.
Prints, per model: calls, mean input and output tokens per call, mean paid calls per agent per challenge (finished
matches only), and the p95 and max call latency in seconds.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _events(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            out.append(row.get("event", row))
    return out


def calibrate(root: Path, min_cap: int) -> dict[str, dict[str, float]]:
    tokens: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    per_challenge: dict[str, list[float]] = defaultdict(list)
    for path in sorted(root.glob("*.events.jsonl")):
        caps: dict[str, int] = {}
        models: dict[str, str] = {}
        calls: Counter[str] = Counter()
        kinds: Counter[str] = Counter()
        challenges = 1
        for e in _events(path):
            kinds[e["type"]] += 1
            data = e["data"]
            if e["type"] == "match_created":
                rules = data["rules"]
                challenges = rules["rounds"] * rules["challenges_per_round"]
                for a in data["agents"]:
                    caps[a["name"]] = a.get("max_output_tokens") or rules.get("max_output_tokens") or 0
                    models[a["name"]] = a["model"]
            elif e["type"] == "call_charged" and caps.get(data["agent"], 0) >= min_cap:
                tokens[data["model"]].append((data["input_tokens"], data["output_tokens"], data["elapsed_ms"] / 1000))
                calls[data["agent"]] += 1
        if kinds["round_scored"] and not kinds["match_failed"]:
            for agent, n in calls.items():
                per_challenge[models[agent]].append(n / challenges)
    table = {}
    for model, rows in sorted(tokens.items()):
        latency = sorted(r[2] for r in rows)
        table[model] = {
            "calls": len(rows),
            "input_tokens": round(statistics.mean(r[0] for r in rows)),
            "output_tokens": round(statistics.mean(r[1] for r in rows)),
            "calls_per_challenge": round(statistics.mean(per_challenge[model]), 2) if per_challenge[model] else 0.0,
            "p95_s": round(latency[min(len(latency) - 1, int(0.95 * len(latency)))], 1),
            "max_s": round(latency[-1], 1),
        }
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--min-cap", type=int, default=8000)
    args = parser.parse_args()
    for model, row in calibrate(args.root, args.min_cap).items():
        print(model, json.dumps(row))


if __name__ == "__main__":
    main()
