"""$0 reproducibility check for the no-fluke ladder: how often two runs of one plan reach the same conclusions.

Boards are drawn from a stated per-board model (decided-board win chance, tie share), and every match is stopped and
called by the real duplicate stop rule and verdict, so only the board model is assumed. Ladders use Bradley-Terry
strengths per model. Scripted-bot duplicate play (``measure``) checks the scoring that produces those boards.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass

from claude_duel.app.ladder import compare_ladders
from claude_duel.app.ladder_config import LadderConfig
from claude_duel.domain import duplicate as dp
from claude_duel.domain import ladder as ld


def play_match(rule: dp.DuplicateRule, p_win: float, p_tie: float, rng: random.Random) -> tuple[dp.Result, int]:
    """One duplicate match: first's chance to win a decided board is ``p_win``; ``p_tie`` of boards score 0."""
    wins = losses = boards = 0
    while not dp.stop_now(rule, boards, wins, losses):
        boards += 1
        if rng.random() >= p_tie:
            wins, losses = (wins + 1, losses) if rng.random() < p_win else (wins, losses + 1)
    return dp.verdict(rule, wins, losses), boards


@dataclass(frozen=True)
class MatchupRepro:
    p_win: float
    first: float  # share of runs backing the stronger side
    wrong: float  # share backing the weaker side
    repeat_agree: float  # two independent runs back the same side
    mean_boards: float


def matchup(rule: dp.DuplicateRule, p_win: float, p_tie: float, runs: int, seed: str) -> MatchupRepro:
    rng = random.Random(seed)  # noqa: S311 (simulation, not security)
    played = [play_match(rule, p_win, p_tie, rng) for _ in range(runs)]
    first = sum(r == "first" for r, _ in played) / runs
    wrong = sum(r == "second" for r, _ in played) / runs
    return MatchupRepro(p_win, first, wrong, first**2 + wrong**2, sum(n for _, n in played) / runs)


def board_p(strength: Mapping[str, float], a: str, b: str) -> float:
    """Bradley-Terry: a's chance to win a decided board against b."""
    return 1 / (1 + math.exp(strength[b] - strength[a]))


def play_ladder_sim(
    cfg: LadderConfig, strength: Mapping[str, float], p_tie: float, rng: random.Random, run_id: str
) -> tuple[ld.LadderState, int]:
    """The ladder fold over simulated duplicate matches; returns the state and the boards played."""
    rule = cfg.duplicate_rule()
    state = ld.LadderState(
        ladder_id=run_id,
        config={},
        digest="sim",
        a=cfg.sides.a,
        b=cfg.sides.b,
        start=(cfg.start_tier.a, cfg.start_tier.b),
    )
    boards = 0
    while not state.done():
        a, b = state.next_pair()
        result, n = play_match(rule, board_p(strength, a, b), p_tie, rng)
        boards += n
        no = len(state.steps) + 1
        state.steps.append(ld.Step(step_no=no, a=a, b=b, duplicate_id=f"{run_id}-s{no}", result=result, usd=0))
    return state, boards


@dataclass(frozen=True)
class LadderRepro:
    ordering_agree: float  # share of run pairs whose path and every decided result match
    matchup_agree: float  # share of matchups (in either run of a pair) labeled agree
    contradict: float  # share of matchups where the two runs backed opposite sides
    mean_boards: float  # boards per ladder run
    mean_steps: float


def ladder(cfg: LadderConfig, strength: Mapping[str, float], p_tie: float, pairs: int, seed: str) -> LadderRepro:
    rng = random.Random(seed)  # noqa: S311 (simulation, not security)
    agree = labeled = agreed = flipped = boards = steps = 0
    for k in range(pairs):
        (one, n1), (two, n2) = (play_ladder_sim(cfg, strength, p_tie, rng, f"{seed}-{k}-{r}") for r in (1, 2))
        cmp = compare_ladders(one, two)
        agree += cmp.ordering == "agree"
        labeled += len(cmp.matchups)
        agreed += sum(m.verdict == "agree" for m in cmp.matchups)
        flipped += sum(set(m.results) == {"first", "second"} for m in cmp.matchups)
        boards += n1 + n2
        steps += len(one.steps) + len(two.steps)
    n = max(1, labeled)
    return LadderRepro(agree / pairs, agreed / n, flipped / n, boards / (2 * pairs), steps / (2 * pairs))
