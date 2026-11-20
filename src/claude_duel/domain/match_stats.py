"""Board statistics for duplicate play (no-fluke ladder: margin, confidence, sequential stop): pure, seeded, $0.

Board differences are from the first agent's side: > 0 a board win, < 0 a loss, 0 a tie (void boards are 0).
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Sprt = Literal["first", "second", "neither", "continue"]
BOOTSTRAP_DRAWS = 2000


@dataclass(frozen=True)
class Record:
    wins: int
    losses: int
    ties: int


def record(diffs: Sequence[int]) -> Record:
    return Record(sum(d > 0 for d in diffs), sum(d < 0 for d in diffs), sum(d == 0 for d in diffs))


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test on decided boards (ties dropped); 1.0 when nothing was decided."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail: float = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_ci(diffs: Sequence[int], seed: str, draws: int = BOOTSTRAP_DRAWS) -> tuple[float, float]:
    """95% percentile interval of the mean board difference; the RNG is seeded from ``seed`` (replayable)."""
    if not diffs:
        return (0.0, 0.0)
    rng = random.Random(int(hashlib.sha256(f"{seed}:bootstrap".encode()).hexdigest()[:16], 16))  # noqa: S311 (stats)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(draws))
    return (means[int(0.025 * draws)], means[int(0.975 * draws) - 1])


def sprt_bounds(alpha: float, beta: float) -> tuple[float, float]:
    """Wald's (lower, upper) log-likelihood-ratio bounds."""
    return (math.log(beta / (1 - alpha)), math.log((1 - beta) / alpha))


def llr(wins: int, losses: int, p1: float) -> float:
    """Log-likelihood ratio of "this side wins a decided board with p1" against an even match (p = 0.5)."""
    return wins * math.log(2 * p1) + losses * math.log(2 * (1 - p1))


def sprt(wins: int, losses: int, p1: float, alpha: float, beta: float) -> Sprt:
    """Two one-sided SPRTs, one per side: a side is accepted as stronger at the upper bound; both at the lower
    bound means neither is (an even match); otherwise keep playing."""
    lower, upper = sprt_bounds(alpha, beta)
    first, second = llr(wins, losses, p1), llr(losses, wins, p1)
    if first >= upper:
        return "first"
    if second >= upper:
        return "second"
    if first <= lower and second <= lower:
        return "neither"
    return "continue"
