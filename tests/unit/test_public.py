"""domain/public.py: the per-kind allowlist that decides what `duel sync` may store."""

from __future__ import annotations

from claude_duel.domain import events as ev
from claude_duel.domain.flags import derive_flag
from claude_duel.domain.models import Event
from claude_duel.domain.public import public_data

FLAG = derive_flag("s" * 64, "c1")


def _event(kind: str, data: dict[str, object]) -> Event:
    return Event(v=1, seq=0, type=kind, trace_id="t", data=data)


def test_challenge_keeps_only_difficulty() -> None:
    spec = {"title": "t", "difficulty": 2, "source": FLAG, "reference_payload": FLAG, "solver": "x", "description": "d"}
    challenge = {"id": "c1", "round": 1, "author": "alice", "spec": spec, "flag_epoch": 0}
    out = public_data(_event(ev.CHALLENGE_BUILT, {"challenge": challenge}))
    kept = {
        "id": "c1",
        "round": 1,
        "author": "alice",
        "flag_epoch": 0,
        "spec": {"difficulty": 2},
    }  # titles are free text (L1)
    assert out == {"challenge": kept}


def test_forfeited_challenge_has_no_spec() -> None:
    challenge = {"id": "c1", "round": 1, "author": "alice", "spec": None, "forfeit_reason": "r"}
    assert public_data(_event(ev.CHALLENGE_BUILT, {"challenge": challenge}))["challenge"] == challenge


def test_attempts_lose_payload_and_output() -> None:
    attempt = {"payload": FLAG, "output": FLAG, "tokens": 5, "usd_micros": 1}
    out = public_data(_event(ev.ATTACK_ATTEMPTED, {"key": "k", "attempt": attempt}))
    assert out == {"key": "k", "attempt": {"tokens": 5, "usd_micros": 1}}


def test_notes_findings_and_seed_are_dropped() -> None:
    assert public_data(_event(ev.NOTE_RECORDED, {"agent": "a", "round": 1, "text": FLAG})) == {"agent": "a", "round": 1}
    finding = {"agent": "a", "round": 1, "category": "c", "severity": "s", "rule_id": "r", "excerpt": FLAG}
    assert "excerpt" not in str(public_data(_event(ev.FINDING_FLAGGED, {"finding": finding})))
    assert public_data(_event(ev.MATCH_CREATED, {"seed": "x", "framing_text": "y"})) == {}


def test_kept_strings_are_scrubbed_and_unknown_kinds_dropped() -> None:
    out = public_data(_event(ev.MATCH_FAILED, {"reason": f"leaked {FLAG} sk-abcdefghijkl"}))
    assert FLAG not in str(out)
    assert "sk-abc" not in str(out)
    assert public_data(_event("future_kind", {"anything": FLAG})) == {}
