"""$0 reproducibility simulation: the real stop rule calls simulated boards; two-run agreement is measured."""

from __future__ import annotations

from pathlib import Path

from claude_duel.app import ladder_config as lc
from claude_duel.app import repro
from claude_duel.domain.duplicate import DuplicateRule

FINAL = Path(__file__).resolve().parents[2] / "examples" / "ladder-final.toml"
RULE = DuplicateRule(boards_min=16, boards_max=48)


def test_a_wide_gap_repeats_and_noise_rarely_names_a_winner() -> None:
    wide = repro.matchup(RULE, 0.8, 0.25, 1000, "w")
    assert wide.repeat_agree > 0.85
    assert wide.wrong < 0.01
    noise = repro.matchup(RULE, 0.5, 0.25, 1000, "n")
    assert noise.first + noise.wrong < 0.1  # equal sides: a backed winner is a false positive


def test_ladder_simulation_is_seeded_and_never_flips_a_wide_gap() -> None:
    cfg = lc.load(FINAL, {"match.boards_min": 16, "match.boards_max": 48})
    models = [*cfg.sides.a, *cfg.sides.b]
    strength = {m: 3.0 - i for i, m in enumerate(models)}  # every pairing far apart
    one = repro.ladder(cfg, strength, 0.25, 30, "s")
    assert one == repro.ladder(cfg, strength, 0.25, 30, "s")
    assert one.contradict == 0
    assert one.ordering_agree > 0.8
