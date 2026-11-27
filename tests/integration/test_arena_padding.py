"""S17-2 regression (security review 2026-11-26): the author pads its source with whitespace, which the attacker's
vendor tokenizes cheaply. Output units used to be output_tokens x this call's k = (prompt bytes / 4) / input_tokens,
so the opponent multiplied the attacker's stake and clock debit (2.5-2.7x here; 2.5-5.6x in S17-2). Output now
uses the pinned vendor ratio.
The tokenizer stub counts a whitespace run as one token and other text as one token per 4 characters. No model calls."""

from __future__ import annotations

import json
import re

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.domain.models import AgentSpec, MatchState, SandboxLimits
from claude_duel.domain.modes import preset
from claude_duel.ports import Completion, CompletionRequest

_TOKEN = re.compile(r"\s+|\S{1,4}")


def _tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


OUT = 5000  # vendor output tokens per attack reply (reasoning + answer at effort high)


class Attacker:
    name = "fake"

    def __init__(self) -> None:
        self.fake = FakeProvider(skill=0)  # never solves: all three tries get played if the stake allows

    def complete(self, request: CompletionRequest) -> Completion:
        out = self.fake.complete(request)
        tin = _tokens(request.system) + _tokens(request.prompt) + 8
        tout = min(OUT, request.max_tokens) if request.purpose == "attack" else out.output_tokens
        return out.model_copy(update={"input_tokens": tin, "output_tokens": tout})


class PaddingAuthor:
    name = "fake"

    def __init__(self, pad: int) -> None:
        self.fake, self.pad = FakeProvider(skill=0), pad

    def complete(self, request: CompletionRequest) -> Completion:
        out = self.fake.complete(request)
        if request.purpose != "build" or not self.pad:
            return out
        body = json.loads(out.text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        body["source"] = body["source"] + "\n#" + " " * self.pad + "\n"
        return out.model_copy(update={"text": "```json\n" + json.dumps(body) + "\n```"})


def _run(make_engine, pad: int):  # type: ignore[no-untyped-def]
    rules = preset("ladder")
    agents = (AgentSpec(name="alice", model="fake-1"), AgentSpec(name="bob", model="fake-1"))
    state = MatchState(match_id=f"k-{pad}", secret="s" * 64, agents=agents, rules=rules, limits=SandboxLimits())
    state = make_engine({"alice": Attacker(), "bob": PaddingAuthor(pad)}).run(state)
    tries = [a for key, run in state.attempts.items() if key.endswith(":alice") for a in run]
    return state, tries


def test_opponent_padding_does_not_inflate_the_attackers_output_charge(make_engine) -> None:  # type: ignore[no-untyped-def]
    clean, clean_tries = _run(make_engine, 0)
    padded, padded_tries = _run(make_engine, 1800)
    assert len(padded_tries) == len(clean_tries) > 0
    # the padding legitimately adds its own bytes / 4 input units to each try; output is charged at the pinned ratio,
    # so nothing else may grow (with the per-call k, debits grew 2.5-2.7x)
    padding_units = len(clean_tries) * (-(-1810 // 4))
    debit = [sum(t.tokens for t in tries) for tries in (clean_tries, padded_tries)]
    assert debit[1] - debit[0] <= padding_units
    clock = [st.usage_by_agent["alice"].clock_tokens for st in (clean, padded)]
    assert clock[1] - clock[0] <= padding_units
