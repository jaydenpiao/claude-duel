"""``duel experiment stakes``: paired framing micro-matches under one shared, optionally overridden run cap.

The override is per invocation only (design-stakes-modes.md): no config key or env var can raise a cap.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from claude_duel.app.budget import make_override
from claude_duel.app.container import build_budget, build_ledger
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain.errors import BudgetExceededError, ConfigError
from claude_duel.domain.models import FramingRule
from claude_duel.observability import log
from claude_duel.ports import Metrics

_LOG = logging.getLogger(__name__)
ARMS = ("neutral", "competitive", "survival")
STOP_SHARE = 0.95  # stop before a new seed at 95% of the run cap ($1.90 of $2.00), so seeds run whole


def run_stakes(
    settings: Settings,
    prereg: Path,
    seeds: int,
    metrics: Metrics,
    run_max_usd: float | None = None,
    reason: str = "",
) -> dict[str, Any]:
    if not prereg.is_file():
        raise ConfigError(f"experiment: pre-registration {prereg} not found; write it before any spend")
    if seeds < 1:
        raise ConfigError("experiment: --seeds must be at least 1")
    ledger = build_ledger(settings)
    run_id = uuid.uuid4().hex[:12]
    override = None
    if run_max_usd is not None:
        spent = ledger.total_usd()
        override = make_override(settings.budget, "run_max_usd", run_max_usd, reason, spent)
        log(
            _LOG,
            logging.WARNING,
            f"budget override: run_max_usd {override.default:.2f} -> {override.value:.2f} "
            f"(reason={override.reason}, ceiling={override.ceiling:.2f}, "
            f"lifetime spent=${spent:.2f}/${settings.budget.lifetime_max_usd:.0f})",
            run_id=run_id,
        )
        ledger.record_override(run_id, override.value, override.reason)
    budget = build_budget(settings, override)
    matches: list[dict[str, str]] = []
    stopped = ""
    for i in range(seeds):
        if budget.run_usd >= budget.limits.run_max_usd * STOP_SHARE:
            stopped = f"run spend ${budget.run_usd:.4f} reached the stop line before seed {i}"
            break
        seed = f"stakes-{i:02d}"
        for arm in ARMS[i % 3 :] + ARMS[: i % 3]:
            rules = settings.rules.model_copy(update={"framing": FramingRule(kind=arm)})
            arm_settings = settings.model_copy(update={"seed": seed, "rules": rules})
            mid = f"{run_id}-{seed}-{arm}"
            try:
                state = play(arm_settings, mid, metrics, budget=budget)
            except BudgetExceededError as exc:
                stopped = str(exc)
                break
            matches.append({"match_id": mid, "seed": seed, "framing": arm, "stage": state.stage.value})
        if stopped:
            break
    return {
        "run_id": run_id,
        "prereg": str(prereg),
        "matches": matches,
        "stopped": stopped,
        "budget": {
            "run_usd": round(budget.run_usd, 6),
            "run_max_usd": budget.limits.run_max_usd,
            "overrides": [override.model_dump()] if override else [],
        },
    }


def footer(result: dict[str, Any]) -> str:
    """Commentary footer line for an overridden run ("" when the defaults applied)."""
    return "".join(f"run cap overridden to ${o['value']:.2f} ({o['reason']})" for o in result["budget"]["overrides"])
