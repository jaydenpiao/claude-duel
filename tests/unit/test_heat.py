"""Sealed Ledger step 3c: novelty heat (design-core-logic.md 4.3, test 9)."""

from __future__ import annotations

import platform

from hypothesis import given
from hypothesis import strategies as st

from claude_duel.app.agents import build_prompt
from claude_duel.app.replay import _python_warning
from claude_duel.domain import events as ev
from claude_duel.domain.heat import heat, heat_lines, penalty, similarity, tokens
from claude_duel.domain.models import Attack, Challenge, Event, LedgerRule, Validation
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import defense_award, round_heat, score_round
from tests.unit.test_domain import spec

LOOP = """import sys
def check(s):
    total = 0
    for i, ch in enumerate(s):
        total = (total * 31 + ord(ch) ^ i) % 1000003
    return total == 4242
print(check(sys.stdin.read().strip()))
"""
RENAMED = LOOP.replace("total", "acc").replace("31", "37").replace("4242", "17")
OTHER = """import sys, json
class Machine:
    def __init__(self):
        self.stack = []
    def run(self, prog):
        while prog:
            op = prog.pop()
            if op == 'p':
                self.stack.append(len(prog))
            elif op == 'x' and self.stack:
                self.stack[-1] ^= 5
        return self.stack
print(Machine().run(list(json.loads(sys.stdin.read()))) == [3])
"""
RULES = preset("ledger")


LED = RULES.ledger
assert LED is not None


def test_similarity_ignores_names_literals_and_formatting() -> None:
    assert similarity(LOOP, LOOP) == 100
    assert similarity(LOOP, RENAMED) == 100
    assert similarity(LOOP, "\n\n" + LOOP.replace("    ", "\t") + "# note\n") == 100
    assert similarity(LOOP, OTHER) < LED.heat_threshold_pct
    assert tokens("def (:") == ()
    assert similarity("", LOOP) == 0


def test_tokens_are_a_true_preorder_with_ops_and_constant_types() -> None:
    assert tokens("a + 1 < b") == (
        "Expr", "Compare", "Lt", "BinOp", "Add", "Name", "Constant", "int", "Name"
    )  # fmt: skip


def _round(
    src1: str, src2: str, valid1: bool = True
) -> tuple[list[Challenge], dict[str, Validation], dict[str, Attack]]:
    chs = [
        Challenge(id="r1a", round=1, author="a", spec=spec(src1, difficulty=2)),
        Challenge(id="r2a", round=2, author="a", spec=spec(src2, difficulty=2)),
        Challenge(id="r1b", round=1, author="b", spec=spec(src1, difficulty=2)),  # the opponent's never count
    ]
    vals = {c.id: Validation(challenge_id=c.id, valid=valid1 or c.id != "r1a", reason="ok") for c in chs}
    atks = {
        "r2a": Attack(challenge_id="r2a", attacker="b", solved=False, attempts=1, tokens_used=0, reason="r", stake=4000)
    }
    return chs, vals, atks


def test_repeat_source_keeps_the_defense_award_and_logs_a_capped_penalty_line() -> None:
    """Spec test 9: identical source -> full defense award, separate heat_penalty line at the cap (50%)."""
    full = defense_award(spec(LOOP, difficulty=2), 2, RULES.scoring)
    hot = score_round(2, ["a", "b"], *_round(LOOP, LOOP), RULES)
    cold = score_round(2, ["a", "b"], *_round(LOOP, OTHER), RULES)
    lines = round_heat(*_round(LOOP, LOOP)[:2], {"r2a": _round(LOOP, LOOP)[2]["r2a"]}, 2, RULES)
    assert cold["a"] == full
    assert lines == {"r2a": full * 50 // 100}
    assert hot["a"] == full - lines["r2a"]
    assert hot["b"] == cold["b"]
    assert round_heat(*_round(LOOP, OTHER)[:2], {}, 2, RULES) == {}


def test_keystone_doubling_is_untouched_by_heat() -> None:
    chs, vals, atks = _round(LOOP, LOOP)
    full = defense_award(spec(LOOP, difficulty=2), 2, RULES.scoring)
    assert score_round(2, ["a", "b"], chs, vals, atks, RULES, {"a": "r2a"})["a"] == 2 * full - full // 2


def test_penalty_ramp_threshold_and_cap() -> None:
    assert penalty(20, LED.heat_threshold_pct, LED) == 0
    assert penalty(20, 85, LED) == min(10, 20 * 15 // 30)
    assert penalty(20, 100, LED) == 10
    assert penalty(20, 69, LED) == 0


def test_at_threshold_logs_a_zero_line_and_below_logs_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    chs, vals, atks = _round(LOOP, LOOP)
    by = {"r2a": atks["r2a"]}
    for sim, want in ((LED.heat_threshold_pct, {"r2a": 0}), (LED.heat_threshold_pct - 1, {})):
        monkeypatch.setattr("claude_duel.domain.heat.similarity", lambda a, b, s=sim: s)
        assert heat_lines(chs, vals, by, 2, LED, lambda c: 20) == want


def test_invalid_challenges_neither_take_nor_give_heat() -> None:
    chs, vals, _ = _round(LOOP, LOOP, valid1=False)
    assert heat(chs, vals, 2, LED) == {}
    vals = {**{c.id: Validation(challenge_id=c.id, valid=True, reason="ok") for c in chs}}
    vals["r2a"] = Validation(challenge_id="r2a", valid=False, reason="bad")
    assert heat(chs, vals, 2, LED) == {}


def test_heat_window_and_switch() -> None:
    chs, vals, _ = _round(LOOP, LOOP)
    old = [c.model_copy(update={"round": 5}) if c.id == "r2a" else c for c in chs]
    assert heat(chs, vals, 2, LedgerRule()) == {"r2a": 100}
    assert heat(old, vals, 5, LedgerRule()) == {}  # round 1 is outside a window of 2
    assert heat(chs, vals, 2, LedgerRule(heat_window=0)) == {}


def test_forge_prompt_states_the_heat_rule() -> None:
    text = build_prompt("a", 1, 0, RULES, None)
    assert f">= {LED.heat_threshold_pct}% similar" in text
    assert f"up to {LED.heat_max_pct}%" in text
    assert "HEAT" not in build_prompt("a", 1, 0, preset("classic"), None)


def test_a_no_op_insert_does_not_dodge_heat() -> None:
    padded = LOOP.replace("    total = 0\n", "    total = 0\n    pass\n    _ = None\n")
    assert similarity(LOOP, padded) >= LED.heat_threshold_pct


@given(st.integers(0, 10_000), st.integers(0, 100), st.integers(1, 100), st.integers(0, 100))
def test_penalty_is_bounded_by_the_cap(defense: int, h: int, thr: int, cap: int) -> None:
    rule = LedgerRule(heat_threshold_pct=thr, heat_max_pct=cap)
    p = penalty(defense, h, rule)
    assert 0 <= p <= defense * cap // 100
    if h < thr or h == thr < 100:  # 0 below and at the threshold (a 100% threshold charges the cap at 100%)
        assert p == 0


@given(st.sampled_from([LOOP, RENAMED, OTHER]), st.sampled_from([LOOP, RENAMED, OTHER]), st.integers(1, 9))
def test_heat_lines_never_exceed_the_cap(src1: str, src2: str, d: int) -> None:
    chs, vals, atks = _round(src1, src2)
    lines = heat_lines(chs, vals, atks, 2, LED, lambda ch: d * 10)
    assert all(0 <= x <= d * 10 * LED.heat_max_pct // 100 for x in lines.values())


def test_replay_warns_on_a_python_minor_mismatch() -> None:
    created = Event(seq=1, type=ev.MATCH_CREATED, trace_id="t", data={"python": "3.0.0"})
    assert _python_warning([created])
    same = created.model_copy(update={"data": {"python": platform.python_version()}})
    assert _python_warning([same]) == []
