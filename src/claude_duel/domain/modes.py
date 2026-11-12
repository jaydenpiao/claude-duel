"""Named rule presets (ADR 0007). A mode is plain ``Rules`` data; config overrides any field on top."""

from claude_duel.domain.models import ClockRule, DeepRule, LedgerRule, MemoryRule, Rules, ScoringRule, WalletRule

MODES: dict[str, Rules] = {
    "classic": Rules(),
    "quick": Rules(mode="quick", rounds=1, challenges_per_round=1),
    "gated": Rules(mode="gated", difficulty_gate=True),
    "checked": Rules(
        mode="checked",
        challenge_form="check",
        max_format_retries=3,
        variety=True,
        early_bonus_attempts=2,
        retry_hint=True,
        retry_feedback=True,
    ),
    # Scored modes (#67) run on a token clock, so provider latency never decides a result; the wall-clock
    # match_deadline_s stays only as a safety backstop that fails (never scores) a match.
    "efficiency": Rules(
        mode="efficiency",
        scoring=ScoringRule(kind="efficiency"),
        clock=ClockRule(bank_tokens=60_000, increment_tokens=500),
    ),
    "capped": Rules(mode="capped", wallet=WalletRule(per_agent_tokens=30_000)),
    "blitz": Rules(
        mode="blitz", clock=ClockRule(bank_tokens=40_000, increment_tokens=500), scoring=ScoringRule(kind="timed")
    ),
    # Sealed Ledger (design-core-logic.md): stakes, keystone, seeded terrain and novelty heat.
    "ledger": Rules(
        mode="ledger",
        challenges_per_round=3,
        max_attempts=3,
        scoring=ScoringRule(kind="ledger"),
        ledger=LedgerRule(keystone=True, terrain="seeded", heat_window=2, heat_vs_opponent=True),
        clock=ClockRule(bank_tokens=60_000, increment_tokens=500),  # scored modes run on a token clock (#67)
        memory=MemoryRule(kind="summary"),
    ),
}
# Deep layer phase 1 (design-deep-layer.md): ledger plus the Ghost Codex.
MODES["ledger_deep"] = MODES["ledger"].model_copy(update={"mode": "ledger_deep", "deep": DeepRule(ghosts=True)})


def preset(name: str) -> Rules:
    """The preset called ``name``; unknown names list the known ones."""
    if name not in MODES:
        raise KeyError(f"unknown mode {name!r}; known: {', '.join(sorted(MODES))}")
    return MODES[name]


def pin_opponent_heat(rules: Rules) -> Rules:
    """Config rules -> an explicit ``heat_vs_opponent`` for every new heat-on log (S13-5): the mode preset's value,
    else False. ``None`` stays only in logs written before the flag, so store history never decides it."""
    led = rules.ledger
    if led is None or not led.heat_window or led.heat_vs_opponent is not None:
        return rules
    base = MODES.get(rules.mode)
    default = base is not None and base.ledger is not None and base.ledger.heat_vs_opponent is True
    return rules.model_copy(update={"ledger": led.model_copy(update={"heat_vs_opponent": default})})
