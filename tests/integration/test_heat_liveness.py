"""A legal source pair past the heat work limit scores on a labeled bound; the match completes and resumes."""

from __future__ import annotations

import json
import re

import pytest

from claude_duel.adapters.providers.fake import TEMPLATES, FakeProvider
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.heat import PAIR_CELLS, _dp, _overlap, _profile, heat_scan, reset_heat_cache
from claude_duel.domain.memory import summarize
from claude_duel.domain.models import MatchState, Stage
from claude_duel.domain.modes import preset
from claude_duel.ports import Completion, CompletionRequest

_SUM = next(t for t in TEMPLATES if t.key == "sum")
_ROUND = re.compile(r"^ROUND: (\d+)", re.M)
# Round 1 is the shipped template. Rounds 2 and 3 append straight-line statements the flag path still runs.
SOURCES = {1: _SUM.source, 2: _SUM.source + "a = a + 1\n" * 230, 3: _SUM.source + "a = 1 + a\n" * 330}


class Grower:
    """Fake builder that writes ``SOURCES[round]``; attacks are the plain fake. Optionally stops once in round 3."""

    name = "grower"

    def __init__(self, crash: bool) -> None:
        self.inner, self.crash, self.round = FakeProvider(skill=2), crash, 1

    def complete(self, request: CompletionRequest) -> Completion:
        if m := _ROUND.search(request.prompt):  # build and allocate prompts name the round; attacks follow them
            self.round = int(m.group(1))
        if request.purpose == "build":
            body = {
                "title": f"Arithmetic gate {self.round}",
                "description": "Find the input that reveals the flag.",
                "source": SOURCES[self.round],
                "reference_payload": _SUM.payload,
                "difficulty": 2,
            }
            text = "```json\n" + json.dumps(body) + "\n```"
            return Completion(text=text, input_tokens=len(request.prompt) // 4, output_tokens=len(text) // 4)
        if self.crash and self.round == 3 and request.purpose == "attack":
            self.crash = False
            raise RuntimeError("power cut")
        return self.inner.complete(request)


def _state(new_state) -> MatchState:  # type: ignore[no-untyped-def]
    state: MatchState = new_state("live")
    state.rules = preset("ledger").model_copy(update={"rounds": 3})
    return state


def test_fixture_is_over_the_pair_limit_and_its_bound_is_not_exact() -> None:
    left, right = _profile(SOURCES[2]), _profile(SOURCES[3])
    assert left is not None
    assert right is not None
    assert left.tokens is not None
    assert right.tokens is not None
    assert (left.length, right.length) == (1683, 2383)
    assert left.length * right.length == 4_010_589 > PAIR_CELLS
    exact = 100 * _dp(left.tokens, right.tokens) // right.length  # test-only, outside the match
    ceiling = 100 * _overlap(left.counts, right.counts) // right.length
    assert (exact, ceiling) == (65, 70)  # the match charges the ceiling and labels it a bound


def test_over_budget_round_completes_resumes_and_replays(make_engine, new_state, store, event_log) -> None:  # type: ignore[no-untyped-def]
    reset_heat_cache()
    rules = preset("ledger").model_copy(update={"rounds": 3})
    for r, src in SOURCES.items():  # every round's source fits that round's cap
        assert len(src.encode()) <= rules.max_source_bytes(r)
    bob = FakeProvider(skill=0)  # holds nothing back: guesses never capture, so alice's builds are defended
    with pytest.raises(RuntimeError, match="power cut"):
        make_engine({"alice": Grower(crash=True), "bob": bob}).run(_state(new_state))
    stopped = store.load("live")
    assert stopped.stage is Stage.FAILED
    assert stopped.round == 3
    assert 3 not in stopped.scored_rounds
    done = make_engine({"alice": Grower(crash=False), "bob": bob}).run(stopped)
    assert done.stage is Stage.COMPLETE
    assert done.failure is None
    for ch in done.challenges.values():  # admitted by the offline sandbox, round by round
        if ch.author == "alice":
            assert ch.spec is not None
            assert ch.spec.source == SOURCES[ch.round]
            assert done.validations[ch.id].valid
    events = event_log.read("live")
    scored = {int(str(e.data["round"])): e.data for e in events if e.type == ev.ROUND_SCORED}
    assert sorted(scored) == [1, 2, 3]
    bound = scored[3].get("heat_bound")
    alice3 = sorted(c.id for c in done.round_challenges(3) if c.author == "alice")
    assert bound == alice3
    assert all(scored[3]["heat"][cid] == 0 for cid in alice3)  # the bound sits at the threshold: no charge
    assert "heat_bound" not in scored[1]
    assert "heat_bound" not in scored[2]
    assert not [e for e in events if e.type == ev.MATCH_FAILED and "heat" in str(e.data)]
    led = done.rules.ledger
    assert led is not None
    hot = heat_scan(done.challenges.values(), done.validations, 3, led, done.heat_refs)
    assert {cid: (hot[cid].pct, hot[cid].exact) for cid in alice3} == dict.fromkeys(alice3, (70, False))
    assert all(h.exact for cid, h in hot.items() if cid not in alice3)  # bob's repeated templates stay exact
    assert "heat <=70% (bound)" in summarize(done, "alice", 4, done.rules.memory)
    report = replay(done, events)
    assert report.consistent, report.mismatches
    forged = [
        e.model_copy(update={"data": {**e.data, "heat_bound": []}})
        if e.type == ev.ROUND_SCORED and e.data["round"] == 3
        else e
        for e in events
    ]
    assert "round 3 heat tampered" in replay(done, forged).mismatches
