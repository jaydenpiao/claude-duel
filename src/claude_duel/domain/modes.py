"""Named rule presets (ADR 0007). A mode is plain ``Rules`` data; config overrides any field on top."""

from claude_duel.domain.models import ClockRule, Rules, ScoringRule, SolvabilityRule, WalletRule

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
        solvability=SolvabilityRule(enabled=True),
        attacker_solver=True,  # design rec R3: attackers may run a solver too, so brute-force-only puzzles stop paying
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
}


def preset(name: str) -> Rules:
    """The preset called ``name``; unknown names list the known ones."""
    if name not in MODES:
        raise KeyError(f"unknown mode {name!r}; known: {', '.join(sorted(MODES))}")
    return MODES[name]
