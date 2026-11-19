"""An attacker must not void an honest but superlinear check with a max-length payload: validation probes that
length too, and only timeouts within it are refunded and count toward the void."""

from __future__ import annotations

from claude_duel.app import engine as engine_mod
from claude_duel.app.engine import ATTACK_TIMEOUT
from claude_duel.domain.models import LONG_TIMEOUT, AttackAttempt
from claude_duel.domain.probes import MAX_PAYLOAD_CHARS, long_probe
from tests.helpers import ONE, Builds
from tests.integration.test_attack_timeouts import SPIN, SPIN_PAYLOAD, AttackPayload

SRC = (
    "import sys\n"
    "s = sys.stdin.read().strip()\n"
    "acc = 0\n"
    "for i in range(len(s)):\n"
    "    for j in range(len(s)):\n"
    "        acc = (acc * 31 + (ord(s[i]) ^ ord(s[j]))) % 1000003\n"
    "if s.isdigit() and int(s) * 7 == 21 and len(s) == 7 and acc >= 0:\n    print(FLAG)\n"
    "else:\n    print('no')\n"
)


def test_quadratic_check_is_rejected_at_validation_not_voided_by_the_attacker(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    bob = Builds(source=SRC, reference_payload="0000003")
    attacker = AttackPayload("9" * MAX_PAYLOAD_CHARS)
    state = make_engine({"alice": attacker, "bob": bob}).run(new_state(**ONE))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    v = state.validations[ch.id]
    assert not v.valid
    assert (v.reason or "").startswith("too slow: junk input")  # caught by the long probe, before any attack
    assert v.reason != ATTACK_TIMEOUT
    assert attacker.attacks == 0


def test_long_probe_matches_the_reference_class() -> None:
    assert long_probe("0000003") == "1" * MAX_PAYLOAD_CHARS
    assert long_probe("") == "b" * MAX_PAYLOAD_CHARS


def test_long_timeout_is_a_spent_try() -> None:
    spent = AttackAttempt(payload="x", output=LONG_TIMEOUT, tokens=1)
    assert not spent.timeout


def test_timeout_past_the_probed_length_is_spent_not_refunded(make_engine, new_state, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(engine_mod, "MAX_PAYLOAD_CHARS", len(SPIN_PAYLOAD) - 1)  # stdin is now "too long"
    attacker = AttackPayload(SPIN_PAYLOAD)
    state = make_engine({"alice": attacker, "bob": Builds(source=SPIN, reference_payload="0000003")}).run(
        new_state(**ONE)
    )
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[ch.id].valid  # never voided by long-input timeouts
    attack = state.attacks[f"{ch.id}:alice"]
    assert attack.attempts == state.max_attempts("alice")  # every try spent
    assert all(a.output == LONG_TIMEOUT and not a.timeout for a in state.attempts[f"{ch.id}:alice"])
