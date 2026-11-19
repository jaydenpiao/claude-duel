"""A check that hangs only on same-shape wrong inputs differing at the first char
must not stand: probes now include index 0, and attack-time timeouts refund the try
and void the challenge after two wall/signal kills."""

from __future__ import annotations

import json
import time

from claude_duel.app.engine import ATTACK_TIMEOUT, TIMEOUTS_TO_VOID
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.models import Stage
from claude_duel.ports import Completion, CompletionRequest
from tests.helpers import ONE, Builds, run_bob

SRC = (
    "import sys\n"
    "s = sys.stdin.read().strip()\n"
    "if s.isdigit() and int(s) * 7 == 21 and len(s) == 7:\n    print(FLAG)\n"
    "elif s.isdigit() and len(s) == 7 and s[0] != '0':\n    n = 0\n    for i in range(10**10):\n        n += i\n"
    "else:\n    print('no')\n"
)

SPIN = (
    "import sys\n"
    "s = sys.stdin.read().strip()\n"
    "if s.isdigit() and int(s) * 7 == 21 and len(s) == 7:\n    print(FLAG)\n"
    "elif s == 'SPINME!':\n    n = 0\n    for i in range(10**10):\n        n += i\n"
    "else:\n    print('no')\n"
)

SPIN_PAYLOAD = "SPINME!"


class AttackPayload(Builds):
    """Builds a challenge, then always attacks with a fixed payload."""

    name = "attack-payload"

    def __init__(self, payload: str, **spec: object) -> None:
        super().__init__(**spec)
        self.payload, self.attacks = payload, 0

    def complete(self, request: CompletionRequest) -> Completion:
        if request.purpose == "attack":
            self.attacks += 1
            body = "```json\n" + json.dumps({"payload": self.payload}) + "\n```"
            return Completion(text=body, input_tokens=10, output_tokens=10)
        return super().complete(request)


def test_first_char_variant(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    t = time.monotonic()
    state, ch = run_bob(make_engine, new_state, source=SRC, reference_payload="0000003")
    v = state.validations[ch.id]
    assert not v.valid
    assert "too slow" in (v.reason or "")
    assert time.monotonic() - t < 20


def test_attack_timeouts_are_refunded_void_the_challenge_and_replay(make_engine, new_state, store, event_log) -> None:  # type: ignore[no-untyped-def]
    bob = Builds(source=SPIN, reference_payload="0000003")
    attacker = AttackPayload(SPIN_PAYLOAD)
    engine = make_engine({"alice": attacker, "bob": bob})
    state = engine.run(new_state(**ONE))
    ch = next(c for c in state.challenges.values() if c.author == "bob")
    assert state.validations[ch.id].valid is False
    assert "too slow" in (state.validations[ch.id].reason or "")
    key = f"{ch.id}:alice"
    attack = state.attacks[key]
    assert not attack.solved
    assert attack.attempts == 0  # both timeouts were refunded
    assert attack.reason == ATTACK_TIMEOUT
    history = state.attempts[key]
    assert len(history) == TIMEOUTS_TO_VOID
    assert all(a.timeout for a in history)
    assert all(a.logged().get("timeout") is True for a in history)
    timeouts = [e for e in event_log.read(state.match_id) if e.type == ev.CHALLENGE_TIMEOUT]
    assert len(timeouts) == TIMEOUTS_TO_VOID
    assert timeouts[0].data["voided"] is False
    assert timeouts[-1].data["voided"] is True
    assert attacker.attacks == TIMEOUTS_TO_VOID  # stopped after the void
    assert state.scores["bob"] == -state.rules.scoring.invalid_penalty * ch.round
    report = replay(state, event_log.read(state.match_id))
    assert report.consistent
    again = engine.run(store.load(state.match_id))
    assert again.stage is Stage.COMPLETE
    assert again.usage.calls == state.usage.calls
    assert attacker.attacks == TIMEOUTS_TO_VOID  # resume does not re-pay
    assert again.validations[ch.id].reason == state.validations[ch.id].reason
    assert again.scores == state.scores
