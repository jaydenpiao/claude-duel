"""S13-4 and liveness: a large unrelated pair is compared exactly, and a pair past the work budget is heat unchecked
(never charged, never a failure); the match completes, resumes and replays."""

from __future__ import annotations

import json
import re

import pytest

from claude_duel.adapters.providers.fake import TEMPLATES, FakeProvider
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.heat import HeatScorer
from claude_duel.domain.heat_bound import lcs, work
from claude_duel.domain.heat_tokens import profile
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


def test_large_unrelated_pair_is_exact_where_the_old_ceiling_charged() -> None:
    left, right = profile(SOURCES[2]), profile(SOURCES[3])
    assert left is not None
    assert right is not None
    assert left.tokens is not None
    assert right.tokens is not None
    assert (left.length, right.length) == (1683, 2383)
    assert left.length * right.length == 4_010_589  # past the old 4M-cell DP limit
    exact = 100 * lcs(left.tokens, right.tokens) // right.length
    ceiling = 100 * sum(min(n, right.counts.get(t, 0)) for t, n in left.counts.items()) // right.length
    assert (exact, ceiling) == (65, 70)  # the old multiset ceiling reached the threshold; the LCS does not
    sc = HeatScorer()
    rule = preset("ledger").ledger
    assert rule is not None
    assert sc.similarity(SOURCES[2], SOURCES[3], rule) == (65, True)
    assert sc.work == work(left.length, right.length)


def test_over_budget_round_completes_resumes_and_replays(make_engine, new_state, store, event_log, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Only round 3's non-copy pair needs the LCS; a budget below it leaves that pair heat unchecked.
    monkeypatch.setattr("claude_duel.domain.heat.WORK_BUDGET", 1000)
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
    alice3 = sorted(c.id for c in done.round_challenges(3) if c.author == "alice")
    unchecked = scored[3].get("heat_unchecked", [])
    assert set(alice3) <= set(unchecked)
    assert not set(alice3) & set(scored[3].get("heat", {}))  # nothing compared reached the threshold: no charge
    assert "heat_unchecked" not in scored[1]
    assert not [e for e in events if e.type == ev.MATCH_FAILED and "heat" in str(e.data)]
    led = done.rules.ledger
    assert led is not None
    hot = HeatScorer().scan(done.challenges.values(), done.validations, 3, led, done.heat_refs)
    assert all(not hot[cid].exact and hot[cid].pct < led.heat_threshold_pct for cid in alice3)
    assert sorted(cid for cid, h in hot.items() if not h.exact) == unchecked
    # a charged line is an exact similarity at the threshold or over (bob repeats templates), never a bound
    assert all(hot[cid].pct >= led.heat_threshold_pct for cid in scored[3].get("heat", {}))
    assert "heat unchecked" in summarize(done, "alice", 4, done.rules.memory)
    report = replay(done, events)
    assert report.consistent, report.mismatches
    forged = [
        e.model_copy(update={"data": {**e.data, "heat_unchecked": []}})
        if e.type == ev.ROUND_SCORED and e.data["round"] == 3
        else e
        for e in events
    ]
    assert "round 3 heat tampered" in replay(done, forged).mismatches
