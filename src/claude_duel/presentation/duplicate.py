"""Board table and margin for `duel duplicate` (plain text or JSON; pure)."""

from __future__ import annotations

from collections import Counter

from claude_duel.domain.duplicate import DuplicateState


def duplicate_view(state: DuplicateState, verified: bool, mismatches: list[str]) -> dict[str, object]:
    """JSON-ready summary: boards, W-L-T, margin p (the call), sign-test p, bootstrap CI, SPRT, spend and result."""
    r, done = state.record(), state.done()
    first, second = (a.name for a in state.agents)
    return {
        "duplicate_id": state.duplicate_id,
        "sides": [first, second],
        "boards": [b.model_dump() for b in state.boards],
        "record": {"wins": r.wins, "losses": r.losses, "ties": r.ties},
        "margin": sum(state.diffs()),
        "stop": state.rule.stop,
        "margin_p": round(state.margin_p(), 4),
        "p_value": round(state.p_value(), 4),
        "ci95": [round(x, 2) for x in state.ci()],
        "sprt": state.sprt(),
        "spent_usd": round(state.spent_usd(), 4),
        "cap_usd": state.rule.match_max_usd,
        "capped": state.capped,
        "cap_reason": state.cap_reason,
        "truncated": truncated(state),
        "result": state.result() if done else "unfinished",
        "winner": state.winner() if done else None,
        "verified": verified,
        "mismatches": mismatches,
    }


def truncated(state: DuplicateState) -> dict[str, int]:
    """Per agent: twice-truncated builds and attack tries over all boards (scored like refusals, never voided)."""
    total = sum((Counter(b.truncated) for b in state.boards), Counter[str]())
    return {a.name: total[a.name] for a in state.agents}


def duplicate_lines(state: DuplicateState, verified: bool, mismatches: list[str]) -> list[str]:
    first, second = (a.name for a in state.agents)
    lines = [f"duplicate {state.duplicate_id}: {first} vs {second} (board diff is {first}'s side)"]
    for b in state.boards:
        lines.append(f"board {b.board_no}: {b.diff:+d} · raw {b.raw:+d} · ${b.usd:.4f}")
    r, (lo, hi) = state.record(), state.ci()
    lines.append(
        f"record {r.wins}-{r.losses}-{r.ties} · margin {sum(state.diffs()):+d} (p={state.margin_p():.4f})"
        f" · sign p={state.p_value():.4f}"
        f" · 95% CI [{lo:.2f}, {hi:.2f}] · SPRT {state.sprt()}"
        f" · ${state.spent_usd():.4f}/${state.rule.match_max_usd}"
        + (f" · CAPPED ({state.cap_reason or 'cap'})" if state.capped else "")
    )
    cut = truncated(state)
    if any(cut.values()):
        lines.append("truncated (scored): " + ", ".join(f"{name} {n}" for name, n in cut.items()))
    if state.done():
        winner = state.winner()
        lines.append(f"result: {state.result()}" + (f" · winner {winner}" if winner else ""))
    else:
        lines.append("result: unfinished (run again to resume)")
    lines.append("verified ✓" if verified else "verified ✗ " + ", ".join(mismatches))
    return lines
