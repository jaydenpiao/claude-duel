"""Between-round memory (#19)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain import memory
from claude_duel.domain.flags import derive_flag
from claude_duel.domain.models import AgentSpec, MatchState, MemoryRule, Rules, SandboxLimits
from claude_duel.ports import Completion, CompletionRequest

ON = {"kind": "summary"}


class Recorder:
    name = "recorder"

    def __init__(self, skill: int = 2) -> None:
        self.fake, self.prompts = FakeProvider(skill=skill), []

    def complete(self, request: CompletionRequest) -> Completion:
        self.prompts.append(request.prompt)
        return self.fake.complete(request)


def _played(make_engine, new_state, mem: dict[str, object]):  # type: ignore[no-untyped-def]
    alice, bob = Recorder(2), Recorder(5)
    state = make_engine({"alice": alice, "bob": bob}).run(new_state(rounds=3, memory=mem))
    return state, alice, bob


def test_off_by_default_keeps_prompts_free_of_memory(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    _, alice, bob = _played(make_engine, new_state, {})
    assert not any("MEMORY" in p for p in alice.prompts + bob.prompts)


def test_rounds_after_the_first_see_fenced_memory(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, alice, _ = _played(make_engine, new_state, ON)
    round1 = [p for p in alice.prompts if "ROUND: 1 of" in p or "CHALLENGE: r1-" in p]
    later = [p for p in alice.prompts if "ROUND: 3 of" in p or "CHALLENGE: r3-" in p]
    assert round1
    assert not any("MEMORY" in p for p in round1)
    assert later
    assert all("MEMORY of earlier rounds" in p and "Round 2:" in p for p in later)
    theirs = next(c for c in state.challenges.values() if c.author == "bob" and c.round == 1)
    assert theirs.spec is not None
    assert theirs.spec.source.splitlines()[0] in later[0]  # opponent source is shown after the round


def test_summary_is_flag_free_and_never_shows_solvers(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    state, _, _ = _played(make_engine, new_state, ON)
    for agent in ("alice", "bob"):
        text = memory.summarize(state, agent, 3, MemoryRule(kind="summary"))
        assert all(derive_flag(state.secret, cid) not in text for cid in state.challenges)
        assert all(c.spec is None or not c.spec.solver or c.spec.solver not in text for c in state.challenges.values())


def test_summary_replays_identically(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state, _, _ = _played(make_engine, new_state, ON)
    folded = ev.fold(state.match_id, event_log.read(state.match_id))
    rule = MemoryRule(kind="summary")
    assert memory.summarize(folded, "alice", 2, rule) == memory.summarize(state, "alice", 2, rule)


@settings(max_examples=25, deadline=None)
@given(st.integers(100, 1500), st.integers(0, 900))
def _cap_property(max_tokens: int, source_tokens: int) -> None:
    state = _CACHE["state"]
    assert isinstance(state, MatchState)
    rule = MemoryRule(kind="summary", max_tokens=max_tokens, source_tokens=source_tokens)
    text = memory.summarize(state, "alice", 3, rule)
    assert 0 < memory.est_tokens(text) <= max_tokens
    if memory.OMITTED in text:
        assert text.index(memory.OMITTED) == 0  # oldest rounds go first


_CACHE: dict[str, object] = {}


def test_prepare_cap_fixture(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    _CACHE["state"], _, _ = _played(make_engine, new_state, ON)
    _cap_property()


def test_technique_tags() -> None:
    assert memory.technique("import hashlib\nx = hashlib.sha256(b'a')") == "hash-check"
    assert memory.technique("stack = []\nstack.pop()") == "vm"
    assert memory.technique('grid = ["S.G"]') == "maze"
    assert memory.technique("x = sum(ord(c) for c in 'ab')") == "checksum"
    assert memory.technique("def (:") == "other"


SCRATCH = {"kind": "summary+scratchpad"}
LEAK = "DUEL{" + "ab" * 16 + "}"


class Notetaker(Recorder):
    """Fake provider that adds a private note (with a flag-shaped leak attempt) to every reply."""

    def complete(self, request: CompletionRequest) -> Completion:
        out = super().complete(request)
        rnd = request.prompt.count("Round ")  # differs per round, so the last note is identifiable
        note = f'{{"notes": "alice-plan-{rnd} {LEAK}", '
        return out.model_copy(update={"text": out.text.replace("{", note, 1)})


def test_scratchpad_notes_are_private_redacted_and_replayed(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Notetaker(2), Recorder(5)
    engine = make_engine({"alice": alice, "bob": bob})
    state = engine.run(new_state(rounds=3, memory=SCRATCH))
    assert set(state.notes) == {"alice"}
    assert sorted(state.notes["alice"]) == [1, 2, 3]
    assert all(LEAK not in t and "alice-plan-" in t for t in state.notes["alice"].values())
    later = [p for p in alice.prompts if "ROUND: 3 of" in p]
    assert later
    assert all("Your scratchpad (round 2, private)" in p for p in later)
    assert all("private note to yourself" in p for p in alice.prompts)
    assert not any("alice-plan-" in p for p in bob.prompts)
    events = event_log.read(state.match_id)
    assert any(e.type == ev.NOTE_RECORDED for e in events)
    assert replay(state, events).consistent


def test_summary_kind_ignores_notes(make_engine, new_state) -> None:  # type: ignore[no-untyped-def]
    alice, bob = Notetaker(2), Recorder(5)
    state = make_engine({"alice": alice, "bob": bob}).run(new_state(rounds=2, memory=ON))
    assert state.notes == {}
    assert not any("private note" in p for p in alice.prompts)


def test_empty_scratchpad_line() -> None:
    s = MatchState(
        match_id="m", agents=(AgentSpec(name="a"), AgentSpec(name="b")), rules=Rules(), limits=SandboxLimits()
    )
    assert "Your scratchpad: (empty)" in memory.summarize(s, "a", 1, MemoryRule(kind="summary+scratchpad"))
