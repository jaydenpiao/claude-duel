"""The arena shows each player's ledger stakes and revealed keystone."""

from rich.console import Console

from claude_duel.domain import events as ev
from claude_duel.domain.models import MatchState
from claude_duel.domain.modes import preset
from claude_duel.presentation.arena import ArenaView, frame


def test_arena_panel_shows_stakes_and_keystone(make_engine, new_state, event_log) -> None:  # type: ignore[no-untyped-def]
    state: MatchState = new_state("al")
    rules = preset("ledger")
    assert rules.ledger is not None
    state.rules = rules.model_copy(update={"rounds": 1, "ledger": rules.ledger.model_copy(update={"keystone": True})})
    make_engine().run(state)
    console = Console(width=100, record=True, file=open("/dev/null", "w"))  # noqa: SIM115
    view = ArenaView(console, "al", 50.0, lambda: 0.0)
    events = event_log.read("al")
    for e in events:
        view(e)
    view.close()
    assert view.model.ledger["alice"].startswith("r1 stakes 4k·4k·4k")
    assert "bob: r1 stakes 4k·4k·4k" in "".join(seg.text for seg in console.render(frame(view.model, 100)))
    assert any(e.type == ev.KEYSTONE_REVEALED for e in events)
    assert "keystone" in " ".join(view.model.ledger.values())
