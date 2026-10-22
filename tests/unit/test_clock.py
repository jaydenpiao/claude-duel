"""Token game clock and the timed scorer (#36)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app import allowance
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.models import Event, ScoringRule
from claude_duel.domain.scoring import timed_award
from claude_duel.ports import Completion, CompletionRequest


class Slow:
    """Fake provider whose replies are padded, so they cost more clock tokens, with an arbitrary latency."""

    name = "slow"

    def __init__(self, skill: int, pad: int) -> None:
        self.fake, self.pad, self.calls = FakeProvider(skill=skill), pad, 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        out = self.fake.complete(request)
        return out.model_copy(update={"output_tokens": out.output_tokens + self.pad})


BLITZ = {"clock": {"bank_tokens": 3000, "increment_tokens": 0}, "scoring": {"kind": "timed"}}


def test_flag_fall_forfeits_and_replays(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Slow(2, pad=1500), Slow(5, pad=0)
    state = make_engine({"alice": alice, "bob": bob}).run(new_state(**BLITZ))
    assert allowance.clock_left(state, "alice") is not None
    assert allowance.clock_left(state, "alice") <= 0  # type: ignore[operator]
    assert alice.calls < bob.calls
    reasons = [c.forfeit_reason or "" for c in state.challenges.values() if c.author == "alice" and c.spec is None]
    assert reasons
    assert all(r.startswith("clock:") for r in reasons)
    assert replay(state, event_log.read(state.match_id)).consistent


def test_latency_never_moves_the_clock(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    a = make_engine().run(new_state("a", **BLITZ))
    b = make_engine().run(new_state("b", **BLITZ))
    assert a.scores == b.scores
    assert {k: u.clock_tokens for k, u in a.usage_by_agent.items()} == {
        k: u.clock_tokens for k, u in b.usage_by_agent.items()
    }


def test_increment_and_old_logs(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state(clock={"bank_tokens": 100, "increment_tokens": 10, "input_divisor": 4})
    created = Event(seq=0, type=ev.MATCH_CREATED, trace_id="t", data=ev.created_data(state))
    folded = ev.initial_state("m", created)
    ev.apply(folded, created)
    new = {"agent": "alice", "model": "m", "tokens": 50, "usd": 0.0, "input_tokens": 40, "output_tokens": 10}
    old = {"agent": "alice", "model": "m", "tokens": 50, "usd": 0.0}  # pre-#36 log: whole total is charged
    ev.apply(folded, Event(seq=1, type=ev.CALL_CHARGED, trace_id="t", data=new))
    assert allowance.clock_left(folded, "alice") == 100 + 10 - 20
    ev.apply(folded, Event(seq=2, type=ev.CALL_CHARGED, trace_id="t", data=old))
    assert allowance.clock_left(folded, "alice") == 100 + 20 - 70
    assert allowance.clock_left(new_state(), "alice") is None


@given(full=st.integers(1, 500), a=st.integers(0, 10**6), b=st.integers(0, 10**6))
def test_timed_award_is_a_monotone_discount(full: int, a: int, b: int) -> None:
    rule = ScoringRule(kind="timed")
    lo, hi = sorted((a, b))
    assert 1 <= timed_award(full, hi, rule) <= timed_award(full, lo, rule) <= full
    assert timed_award(full, 0, rule) == full
