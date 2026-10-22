from claude_duel.domain import events as ev
from claude_duel.domain.models import AgentSpec, Event, MatchState, Rules, SandboxLimits

"""Test-only helpers."""


def any_source(source: str) -> bool:
    """Lets tests run arbitrary (exploit) code in the subprocess sandbox. Never used outside tests."""
    return bool(source)


FLAG = "DUEL{0123456789abcdef0123456789abcdef}"


def match_events(*extra: Event) -> list[Event]:
    """A created event for a 1-round alice-vs-bob match, then ``extra``."""
    state = MatchState(
        match_id="m",
        secret="s",
        agents=(AgentSpec(name="alice"), AgentSpec(name="bob")),
        rules=Rules(rounds=1),
        limits=SandboxLimits(),
    )
    return [Event(seq=0, type=ev.MATCH_CREATED, trace_id="t", data=ev.created_data(state)), *extra]


def forfeit_events(reject_reason: str, failure: str) -> list[Event]:
    """alice forfeits, the arena rejects it, the match fails, then is recovered."""
    forfeit = {"id": "c1", "round": 1, "author": "alice", "spec": None, "forfeit_reason": f"malformed {FLAG}"}
    rejected = {"validation": {"challenge_id": "c1", "valid": False, "reason": reject_reason}}
    return match_events(
        Event(seq=1, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "build"}),
        Event(seq=2, type=ev.CHALLENGE_BUILT, trace_id="t", data={"challenge": forfeit}),
        Event(seq=3, type=ev.STAGE_CHANGED, trace_id="t", data={"to": "validate"}),
        Event(seq=4, type=ev.CHALLENGE_VALIDATED, trace_id="t", data=rejected),
        Event(seq=5, type=ev.MATCH_FAILED, trace_id="t", data={"reason": failure}),
        Event(seq=6, type=ev.MATCH_RECOVERED, trace_id="t", data={}),
    )
