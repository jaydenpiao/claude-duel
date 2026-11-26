"""Vendor-neutral arena units: the same text costs every vendor the same stake, clock and output cap, however the
vendor tokenizes it. A stand-in "Claude tokenizer" reports 1.594x the fake's tokens for identical replies."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.replay import replay
from claude_duel.domain import arena_units
from claude_duel.domain import events as ev
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits
from claude_duel.domain.modes import preset
from claude_duel.ports import Completion, CompletionRequest

CLAUDE_RATIO = 1.594  # Claude input tokens over GPT's on identical build prompts (fairness audit 2026-11-19)


class Tokenizer:
    """The fake provider's replies, reported in ``scale`` x its token counts (another vendor's tokenizer)."""

    name = "fake"

    def __init__(self, scale: float) -> None:
        self.fake, self.scale, self.requests = FakeProvider(skill=3), scale, list[CompletionRequest]()

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        out = self.fake.complete(request.model_copy(update={"max_tokens": 1500}))
        scaled = {
            "input_tokens": round(out.input_tokens * self.scale),
            "output_tokens": round(out.output_tokens * self.scale),
        }
        return out.model_copy(update=scaled)


def _state(rules: Rules, provider: str = "fake") -> MatchState:
    agents = (AgentSpec(name="alice", provider=provider, model="fake-1"), AgentSpec(name="bob", model="fake-1"))
    return MatchState(match_id="units-1", secret="s" * 64, agents=agents, rules=rules, limits=SandboxLimits())


def _alice(make_engine, rules: Rules, scale: float):  # type: ignore[no-untyped-def]
    state = make_engine({"alice": Tokenizer(scale), "bob": Tokenizer(1.0)}).run(_state(rules))
    tries = [a.tokens for key, run in state.attempts.items() if key.endswith(":alice") for a in run]
    return state.usage_by_agent["alice"], tries


def test_equal_text_costs_equal_stakes_and_clock_in_arena_units(make_engine, store_root) -> None:  # type: ignore[no-untyped-def]
    rules = preset("ladder").model_copy(update={"arena_ratio": {}})
    assert rules.accounting == "arena"
    gpt, gpt_tries = _alice(make_engine, rules, 1.0)
    for f in store_root.rglob("*"):  # a fresh store, so the second run replays the same match from scratch
        if f.is_file():
            f.unlink()
    claude, claude_tries = _alice(make_engine, rules, CLAUDE_RATIO)
    assert gpt_tries
    assert claude.output_tokens > 1.5 * gpt.output_tokens  # vendor counts differ by the tokenizer ...
    assert claude.clock_tokens == pytest.approx(gpt.clock_tokens, rel=0.02)  # ... arena units do not
    assert claude.tokens == pytest.approx(gpt.tokens, rel=0.02)
    for c, g in zip(claude_tries, gpt_tries, strict=True):  # each attack try debits the same stake
        assert c == pytest.approx(g, rel=0.02, abs=2)


def test_vendor_units_keep_the_old_accounting(make_engine, event_log) -> None:  # type: ignore[no-untyped-def]
    rules = Rules(rounds=1, challenges_per_round=1)
    assert rules.accounting == "vendor"
    state = make_engine({"alice": Tokenizer(1.0), "bob": Tokenizer(1.0)}).run(_state(rules))
    charged = [e.data for e in event_log.read(state.match_id) if e.type == ev.CALL_CHARGED]
    assert charged
    assert all("units_in" not in d and "input_bytes" not in d for d in charged)  # older logs stay byte-identical
    assert all(d["tokens"] == d["input_tokens"] + d["output_tokens"] for d in charged)


def test_arena_units_are_logged_and_replay(make_engine, event_log) -> None:  # type: ignore[no-untyped-def]
    state = make_engine({"alice": Tokenizer(CLAUDE_RATIO), "bob": Tokenizer(1.0)}).run(_state(preset("ladder")))
    charged = [e.data for e in event_log.read(state.match_id) if e.type == ev.CALL_CHARGED]
    assert all(d["tokens"] == d["units_in"] + d["units_out"] and d["input_bytes"] > 0 for d in charged)
    assert replay(state, event_log.read(state.match_id)).consistent


def test_output_caps_are_sent_as_each_vendors_equivalent(make_engine) -> None:  # type: ignore[no-untyped-def]
    rules = preset("ladder")
    alice, bob = Tokenizer(CLAUDE_RATIO), Tokenizer(1.0)
    make_engine({"alice": alice, "bob": bob}).run(_state(rules, provider="anthropic"))
    cap = rules.output_cap(purpose="build")
    sent = {r.max_tokens for r in alice.requests if r.purpose == "build"}
    assert sent == {math.ceil(cap / 0.63)}
    assert {r.max_tokens for r in bob.requests if r.purpose == "build"} == {cap}


@given(
    text=st.integers(min_value=1, max_value=200_000),
    out=st.integers(min_value=0, max_value=24_000),
    scale=st.floats(min_value=0.5, max_value=2.5),
)
def test_units_ignore_the_tokenizer_property(text: int, out: int, scale: float) -> None:
    """Same bytes, same reply: a tokenizer reporting ``scale`` x the tokens is charged the same units (+-1 rounding
    of the scaled counts and +-1 for the ceiling)."""
    base_in = max(1, text // 4)
    ref = arena_units.units(text, base_in, out)
    other = arena_units.units(text, max(1, round(base_in * scale)), round(out * scale))
    assert other[0] == ref[0]
    assert abs(other[1] - ref[1]) <= 2 + ref[1] * 2 / base_in + out * 0.01


def test_vendor_tokens_round_the_cap_up() -> None:
    assert arena_units.vendor_tokens(8000, 0.63) == 12699
    assert arena_units.vendor_tokens(8000, 1.0) == 8000
    assert arena_units.request_bytes("ab", "é", {"k": 1}) == 2 + 2 + len('{"k": 1}')
