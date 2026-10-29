"""Sealed Ledger step 3c: novelty heat (design-core-logic.md 4.3, test 9)."""

from __future__ import annotations

from claude_duel.domain.heat import fingerprint, heat, similarity
from claude_duel.domain.models import Attack, Challenge, LedgerRule, Validation
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import defense_award, score_round
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


def test_fingerprint_ignores_names_and_literals() -> None:
    assert similarity(LOOP, LOOP) == 100
    assert similarity(LOOP, RENAMED) == 100
    assert similarity(LOOP, OTHER) < RULES.ledger.heat_threshold_pct  # type: ignore[union-attr]
    assert fingerprint("def (:") == frozenset()
    assert similarity("", LOOP) == 0


def _round(src1: str, src2: str) -> tuple[list[Challenge], dict[str, Validation], dict[str, Attack]]:
    chs = [
        Challenge(id="r1a", round=1, author="a", spec=spec(src1, difficulty=2)),
        Challenge(id="r2a", round=2, author="a", spec=spec(src2, difficulty=2)),
        Challenge(id="r1b", round=1, author="b", spec=spec(src1, difficulty=2)),  # the opponent's never count
    ]
    vals = {c.id: Validation(challenge_id=c.id, valid=True, reason="ok") for c in chs}
    atks = {
        "r2a": Attack(challenge_id="r2a", attacker="b", solved=False, attempts=1, tokens_used=0, reason="r", stake=4000)
    }
    return chs, vals, atks


def test_repeat_source_halves_the_round_two_defense() -> None:
    """Spec test 9: identical source in rounds 1 and 2 halves round 2's defense; a disjoint AST changes nothing."""
    full = defense_award(spec(LOOP, difficulty=2), 2, RULES.scoring)
    hot = score_round(2, ["a", "b"], *_round(LOOP, LOOP), RULES)
    cold = score_round(2, ["a", "b"], *_round(LOOP, OTHER), RULES)
    assert cold["a"] == full
    assert hot["a"] == full // 2
    assert hot["b"] == cold["b"]


def test_heated_keystone_bonus_is_halved_too() -> None:
    chs, vals, atks = _round(LOOP, LOOP)
    full = defense_award(spec(LOOP, difficulty=2), 2, RULES.scoring)
    assert score_round(2, ["a", "b"], chs, vals, atks, RULES, {"a": "r2a"})["a"] == 2 * (full // 2)


def test_heat_window_and_switch() -> None:
    chs, _, _ = _round(LOOP, LOOP)
    old = [c.model_copy(update={"round": 5}) if c.id == "r2a" else c for c in chs]
    assert heat(chs, 2, LedgerRule()) == {"r2a": 100}
    assert heat(old, 5, LedgerRule()) == {}  # round 1 is outside a window of 2
    assert heat(chs, 2, LedgerRule(heat_window=0)) == {}
