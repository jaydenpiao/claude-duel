"""$0 no-fluke harness (design-no-fluke §5): scripted bots of known strength play duplicate matches through the
real engine and sandbox, so the ladder's stop rule can be measured without a model call.

``measure`` reports how often the stronger bot wins and, for equal bots, the first seat's share of decided boards.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from claude_duel.adapters.providers.scripted import ScriptedProvider
from claude_duel.app.container import build_budget, build_event_log, build_store, make_sandbox
from claude_duel.app.engine import MatchEngine
from claude_duel.app.session import open_match
from claude_duel.config import Settings
from claude_duel.domain import duplicate as dp
from claude_duel.domain.models import AgentSpec, MatchState
from claude_duel.domain.modes import preset
from claude_duel.observability import InMemoryMetrics

SIDES = (AgentSpec(name="strong"), AgentSpec(name="weak"))


@dataclass(frozen=True)
class HarnessResult:
    matches: int
    first: int  # the first bot (``pct_a``) won
    second: int
    undecided: int  # inconclusive or void
    first_seat_share: float  # decided boards won by whoever sat first in the half pair (side bias)
    margin_first: int = 0  # matches where the first bot's summed board margin was positive (not a decision rule)
    margins: tuple[int, ...] = ()  # every scored board's margin, first bot's view (input for --repro --boards-from)

    def first_rate(self) -> float:
        return self.first / max(1, self.matches)


def scripted_half(
    settings: Settings, match_id: str, agents: tuple[AgentSpec, AgentSpec], pcts: dict[str, int]
) -> MatchState:
    """One match between scripted bots (capture chance per agent name) through the real engine and sandbox."""
    s = settings.model_copy(update={"agents": agents})
    store = build_store(s)
    state = open_match(store, s, match_id)
    providers = {a.name: ScriptedProvider(pcts[a.name], f"{s.seed}:{match_id}:{a.name}", str(s.seed)) for a in agents}
    engine = MatchEngine(
        dict(providers),
        make_sandbox(s, agents),
        store,
        build_event_log(s),
        InMemoryMetrics(),
        build_budget(s),
        dict.fromkeys(providers, "fake-1"),
    )
    return engine.run(state)


def measure(
    root: Path, pct_a: int, pct_b: int, *, matches: int, rule: dp.DuplicateRule, seed: str = "h"
) -> HarnessResult:
    """``matches`` duplicate matches of ``rule`` between a ``pct_a`` and a ``pct_b`` bot, ladder rules, $0."""
    rules = preset("ladder")
    settings = Settings(store_dir=root, sandbox="subprocess", rules=rules, seed=seed)
    pcts = {SIDES[0].name: pct_a, SIDES[1].name: pct_b}
    tally = {"first": 0, "second": 0}
    ahead = 0
    margins: list[int] = []
    seat_wins = decided = 0
    for m in range(matches):
        state = dp.DuplicateState(
            duplicate_id=f"{seed}-{pct_a}v{pct_b}-{m}",
            seed=f"{seed}{m}",
            rule=rule,
            agents=SIDES,
            rules=rules,
            limits=settings.limits,
        )
        while not state.done():
            k = state.next_board()
            board_seed = dp.board_seed(state.seed, k)
            halves = tuple(
                scripted_half(
                    settings.model_copy(update={"seed": board_seed}),
                    state.half_id(k, h),
                    dp.half_agents(SIDES, h),
                    pcts,
                )
                for h in (0, 1)
            )
            board = dp.board_of(state, k, board_seed, (halves[0], halves[1]))
            state.boards.append(board)
            for h in halves:  # side bias: who won each half, by seat
                lead, other = (a.name for a in h.agents)
                net = dp.net(h, lead) - dp.net(h, other)
                decided += net != 0
                seat_wins += net > 0
        ahead += sum(state.diffs()) > 0
        margins += state.diffs()
        res = state.result()
        if res in tally:
            tally[res] += 1
    undecided = matches - tally["first"] - tally["second"]
    return HarnessResult(
        matches, tally["first"], tally["second"], undecided, seat_wins / max(1, decided), ahead, tuple(margins)
    )
