"""Live view: reasoning summaries reach call_charged, one line, capped, with every flag redacted."""

from __future__ import annotations

import json

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.providers.openai import _parse_responses
from claude_duel.app.engine import SUMMARY_CHARS, MatchEngine
from claude_duel.domain import events as ev
from claude_duel.domain.flags import REDACTED, flag_of
from claude_duel.ports import Completion, CompletionRequest

ONE = {"rounds": 1, "challenges_per_round": 1}


def test_openai_reasoning_summary_is_parsed() -> None:
    data = {
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan a maze"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "{}"}]},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    c = _parse_responses(data)
    assert (c.text, c.reasoning_summary) == ("{}", "plan a maze")


class Thinks:
    """Fake provider whose every answer carries a reasoning summary that tries to leak the flag."""

    name = "thinks"

    def __init__(self) -> None:
        self.fake = FakeProvider()
        self.leak = ""

    def complete(self, request: CompletionRequest) -> Completion:
        c = self.fake.complete(request)
        return c.model_copy(update={"reasoning_summary": f"I think\n {self.leak} " + "x" * 400})


def test_summary_is_logged_redacted_and_capped(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    thinks = Thinks()
    state = new_state(**ONE)
    thinks.leak = "DUEL{" + "ab" * 16 + "}"  # a flag-shaped string must never survive
    final = make_engine({"alice": thinks, "bob": FakeProvider()}).run(state)
    charged = [e for e in event_log.read(final.match_id) if e.type == ev.CALL_CHARGED and e.data["agent"] == "alice"]
    assert charged
    for e in charged:
        summary = e.data["summary"]
        assert summary.startswith("I think ")
        assert "\n" not in summary
        assert len(summary) <= SUMMARY_CHARS + 1
        assert "DUEL{" not in summary.replace(REDACTED, "")
        assert e.data["purpose"] in {"build", "attack", "repair", "gate"}
    body = json.dumps([e.data for e in event_log.read(final.match_id)])
    assert thinks.leak not in body


def test_summary_redacts_the_real_flag_of_the_match(new_state) -> None:  # type: ignore[no-untyped-def]
    state = new_state(**ONE)
    state.build_errors["c1"] = []
    flag = flag_of(state.secret, "c1", 0)
    out = MatchEngine._summary(state, f"the answer is {flag} or {flag[::-1]}")
    assert flag not in out
    assert flag[::-1] not in out
