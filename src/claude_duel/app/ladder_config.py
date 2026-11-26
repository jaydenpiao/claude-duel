"""The no-fluke ladder plan (design-no-fluke §4.1): a TOML file plus CLI overrides, validated at $0.

Shape and budget are run-time config, never code. Model retirement is checked against the plan's ``run_date``,
never the wall clock, so one plan validates the same way forever.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_duel.app.budget import FAKE_MODEL, check_model_allowed, vendor_of
from claude_duel.config import read_toml
from claude_duel.domain.duplicate import MAX_BOARDS, DuplicateRule
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec, CallCaps, Rules
from claude_duel.domain.modes import preset

RETIRED: dict[str, tuple[date, str]] = {"gpt-5-nano": (date(2026, 12, 11), "gpt-5-mini")}  # model: (from, use)
SIDES = ("a", "b")


class _Plan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Sides(_Plan):
    a: tuple[str, ...] = Field(min_length=1)
    b: tuple[str, ...] = Field(min_length=1)


class Tier(_Plan):
    a: int = 0
    b: int = 0


class MatchPlan(_Plan):
    boards_min: int = 12
    boards_max: int = 48
    # A fixed board count for every pairing: no early stop, and money never ends a match early (the dry run refuses a
    # plan whose boards cannot fit the caps; a hard stop before N leaves the match "capped", with no verdict).
    boards: int | None = None


Basis = Literal["worst", "typical", "calibrated"]


class BudgetPlan(_Plan):
    match_max_usd: float
    ladder_max_usd: float
    # What the dry run checks the caps against. "typical" admits plans whose worst case is over a cap; the per-call
    # hard stop then keeps actual spend under both caps (the run stops cleanly, resumable after raising a cap).
    # "calibrated" prices each model from its measured token use (app/calibration.py) instead of one assumed size.
    basis: Basis = "worst"


class CapsPlan(_Plan):
    build: int
    allocate: int
    attack: int
    max_attempts: int = 3


class LadderConfig(_Plan):
    run_date: date
    sides: Sides
    start_tier: Tier = Tier()
    match: MatchPlan = MatchPlan()
    budget: BudgetPlan
    caps: CapsPlan

    def side(self, name: str) -> tuple[str, ...]:
        return self.sides.a if name == "a" else self.sides.b

    def digest(self) -> str:
        """sha256 of the canonical plan JSON, logged so a resumed or verified ladder proves its plan. Money is left
        out: raising a cap after a hard stop resumes the same ladder, and spend never changes a result."""
        canonical = json.dumps(self.model_dump(mode="json", exclude={"budget"}), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def rules(self) -> Rules:
        """The ``ladder`` preset with this plan's per-call caps and tries."""
        caps = CallCaps(build=self.caps.build, allocate=self.caps.allocate, attack=self.caps.attack)
        base = preset("ladder").model_dump()
        return Rules.model_validate(base | {"max_attempts": self.caps.max_attempts, "call_caps": caps.model_dump()})

    def duplicate_rule(self) -> DuplicateRule:
        """``match.boards`` fixes every pairing at that many boards (no early stop); else the margin rule's range."""
        n = self.match.boards
        return DuplicateRule(
            boards_min=self.match.boards_min if n is None else n,
            boards_max=self.match.boards_max if n is None else n,
            stop="margin" if n is None else "none",
            match_max_usd=self.budget.match_max_usd,
        )


def agents(a: str, b: str) -> tuple[AgentSpec, AgentSpec]:
    """Sides are labeled by model name (``.`` becomes ``-``); a mirror pairing gets ``-a``/``-b``."""
    names = [re.sub(r"[^a-z0-9_-]", "-", m.lower())[:30] for m in (a, b)]
    if names[0] == names[1]:
        names = [f"{names[0]}-a", f"{names[1]}-b"]
    return tuple(  # type: ignore[return-value]
        AgentSpec(name=n, provider=vendor_of(m), model=m) for n, m in zip(names, (a, b), strict=True)
    )


def load(path: Path, overrides: dict[str, Any] | None = None, *, allow_premium: bool = False) -> LadderConfig:
    """Read ``path``, apply ``overrides`` (dotted keys such as ``budget.match_max_usd``; flags beat the file)
    and validate. Every failure is a ``ConfigError`` naming the rule it broke."""
    return parse(read_toml(path), overrides, allow_premium=allow_premium)


def parse(
    data: dict[str, Any], overrides: dict[str, Any] | None = None, *, allow_premium: bool = False
) -> LadderConfig:
    raw = json.loads(json.dumps(data, default=str))  # a deep copy; TOML dates become ISO strings
    if any(isinstance(v, dict) for v in raw.get("caps", {}).values()):
        raise ConfigError("per-call caps must be the same for both sides: use one flat [caps] table")
    for key, value in (overrides or {}).items():
        *path, leaf = key.split(".")
        node = raw
        for part in path:
            node = node.setdefault(part, {})
        node[leaf] = value
    if not raw.get("run_date"):
        raise ConfigError("run_date is required (ISO-8601 date, e.g. 2026-11-20); it drives the model cutoffs")
    if not isinstance(raw["run_date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw["run_date"]):
        raise ConfigError(f"run_date must be an ISO-8601 date (YYYY-MM-DD), got {raw['run_date']!r}")
    try:
        cfg = LadderConfig.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        raise ConfigError(f"bad ladder config: {'.'.join(map(str, err['loc']))}: {err['msg']}") from exc
    check(cfg, allow_premium)
    return cfg


def check(cfg: LadderConfig, allow_premium: bool = False) -> None:
    """The §4.1 validator: models (the same allowlist, premium and route gates as a real run), vendors, tiers,
    boards, money, caps and retirement."""
    _check_models(cfg, allow_premium)
    _check_numbers(cfg)


def _check_models(cfg: LadderConfig, allow_premium: bool) -> None:
    models = [m for s in SIDES for m in cfg.side(s)]
    for m in models:
        check_model_allowed(m, allow_premium)  # the container's gate, so the dry run and the real run agree
    fake = [m == FAKE_MODEL for m in models]
    if any(fake) and not all(fake):
        raise ConfigError(f"{FAKE_MODEL} cannot be mixed with real models")
    for s in SIDES:
        vendor = vendor_of(cfg.side(s)[0])
        for m in cfg.side(s):
            if vendor_of(m) != vendor:
                raise ConfigError(f"model {m!r} is {vendor_of(m)}, but side {s} is {vendor}")
        tier = getattr(cfg.start_tier, s)
        if not 0 <= tier < len(cfg.side(s)):
            raise ConfigError(f"start_tier.{s}={tier} is out of range (side {s} has {len(cfg.side(s))} models)")
    for m in models:
        if m in RETIRED and cfg.run_date >= RETIRED[m][0]:
            cutoff, use = RETIRED[m]
            raise ConfigError(f"{m} is retired from {cutoff} (run_date={cfg.run_date}); use {use}")


def _check_numbers(cfg: LadderConfig) -> None:
    boards = cfg.match
    if boards.boards is not None:
        if {"boards_min", "boards_max"} & boards.model_fields_set:
            raise ConfigError("set either match.boards (fixed) or match.boards_min/boards_max (early stop), not both")
        if not 1 <= boards.boards <= MAX_BOARDS:
            raise ConfigError(f"match.boards must be >= 1 and <= {MAX_BOARDS}")
    elif not 1 <= boards.boards_min <= boards.boards_max <= MAX_BOARDS:
        raise ConfigError(f"boards_min must be >= 1 and <= boards_max, and boards_max <= {MAX_BOARDS}")
    positive = {**cfg.budget.model_dump(exclude={"basis"}), **cfg.caps.model_dump()}
    for key, value in positive.items():
        if value <= 0:
            raise ConfigError(f"{key} must be > 0")
    if cfg.budget.match_max_usd > cfg.budget.ladder_max_usd:
        raise ConfigError("match_max_usd must be <= ladder_max_usd")
    try:
        cfg.rules()
    except ValidationError as exc:
        raise ConfigError(f"bad caps: {exc.errors()[0]['msg']}") from exc
