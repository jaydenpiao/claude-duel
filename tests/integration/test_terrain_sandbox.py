"""Spec test 3, second half: a pre-inverted reference payload still reveals the flag through the real sandbox."""

import pytest

from claude_duel.domain import terrain

KINDS = ["identity", "reverse", "rot13", "swapcase", "xor:1f", "xor:05", "b64", "prefix:3"]


@pytest.mark.parametrize("t", KINDS)
def test_pre_inverted_reference_reveals_the_flag(make_engine, new_state, t: str) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine()
    state = engine.run(new_state(rounds=1, challenges_per_round=1))
    checked = 0
    for ch in state.challenges.values():
        if ch.spec is None or not state.validations[ch.id].valid:
            continue
        sent = terrain.invert(t, ch.spec.reference_payload)
        if sent is None:  # draw_round falls back to identity here; nothing to prove for this kind
            continue
        flag = engine._flag_for(state, ch.id)
        stdin = terrain.apply(t, sent)
        assert stdin == ch.spec.reference_payload
        run = engine._run_challenge(ch.spec.source, stdin, flag, state, "attack")
        assert flag in run.stdout
        raw = engine._run_challenge(ch.spec.source, sent, flag, state, "attack")
        assert sent == ch.spec.reference_payload or flag not in raw.stdout
        checked += 1
    assert checked or t in SHAPED  # these invert only refs of their own shape; proven below


SHAPED = {"b64": "QUJDRA==", "prefix:3": "~~~open"}  # a reference of the shape each kind can invert
SOURCE = "import sys\nif sys.stdin.read() == {ref!r}:\n    print(FLAG)\n"


@pytest.mark.parametrize("t", sorted(SHAPED))
def test_shaped_reference_inverts_through_the_sandbox(make_engine, new_state, t: str) -> None:  # type: ignore[no-untyped-def]
    """b64 and prefix invert only references of their own shape: prove them on one built to fit."""
    engine = make_engine()
    state = engine.run(new_state(rounds=1, challenges_per_round=1))
    ref = SHAPED[t]
    sent = terrain.invert(t, ref)
    assert sent is not None
    assert sent != ref
    flag = engine._flag_for(state, next(iter(state.challenges)))
    source = SOURCE.format(ref=ref)
    assert flag in engine._run_challenge(source, terrain.apply(t, sent), flag, state, "attack").stdout
    assert flag not in engine._run_challenge(source, sent, flag, state, "attack").stdout
