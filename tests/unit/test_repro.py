"""$0 reproducibility simulation: the real stop rule calls simulated boards; two-run agreement is measured."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from claude_duel.app import ladder_config as lc
from claude_duel.app import repro
from claude_duel.domain.duplicate import DuplicateRule


def _load(*a: Any, **k: Any) -> lc.LadderConfig:  # premium plans validate; the premium gate is tested apart
    return lc.load(*a, allow_premium=True, **k)


FINAL = Path(__file__).resolve().parents[2] / "examples" / "ladder-final.toml"
RULE = DuplicateRule()  # the default margin rule, 12-48 boards


def test_a_wide_gap_repeats_and_noise_rarely_names_a_winner() -> None:
    wide = repro.matchup(RULE, repro.coin(0.8, 0.25), 600, "w")
    assert wide.repeat_agree > 0.85
    assert wide.wrong < 0.01
    noise = repro.matchup(RULE, repro.coin(0.5, 0.25), 600, "n")
    assert noise.first + noise.wrong < 0.08  # equal sides: a backed winner is a false positive


def test_resampled_margins_keep_the_sign_unless_symmetric() -> None:

    rng = random.Random(1)
    lead = repro.resample([30, 20, -5])
    assert {lead(rng) for _ in range(200)} == {30, 20, -5}
    null = repro.resample([30], symmetric=True)
    assert {null(rng) for _ in range(200)} == {30, -30}


def test_ladder_simulation_is_seeded_and_never_flips_a_wide_gap() -> None:
    cfg = _load(FINAL, {})
    models = [*cfg.sides.a, *cfg.sides.b]
    strength = {m: 3.0 - i for i, m in enumerate(models)}  # every pairing far apart
    one = repro.ladder(cfg, strength, 0.25, 20, "s")
    assert one == repro.ladder(cfg, strength, 0.25, 20, "s")
    assert one.contradict == 0
    assert one.ordering_agree > 0.8
