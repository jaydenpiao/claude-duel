"""Arch round 3 #8: the difficulty-gate probe is exempt from per-agent wallets, so it never forfeits an author."""

from __future__ import annotations

from claude_duel.adapters.providers.fake import FakeProvider
from tests.integration.test_build_repair import Probe


def test_gate_with_tiny_wallet_does_not_forfeit_builds(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine({"alice": FakeProvider(), "bob": FakeProvider(), "gate": Probe()})
    rules = {"rounds": 1, "challenges_per_round": 1, "difficulty_gate": True, "wallet": {"per_agent_tokens": 1}}
    state = engine.run(new_state(**rules))
    assert state.usage_by_agent["gate"].calls == 2  # probed both challenges, past a wallet it never had
    assert [c.forfeit_reason for c in state.challenges.values()] == [None, None]
