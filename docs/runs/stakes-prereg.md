# Pre-registration: framing A/B/C experiment

Written before any spend. Spec: [design-stakes-modes.md §5](../design/design-stakes-modes.md). Detector and framing: ADR 0008.

## Question
Does the system-prompt framing (`neutral`, `competitive`, `survival`; `rules.framing.kind`, text_version 1)
change build validity, capture rate, defense rate or tamper findings for low-tier models?

## Design
- Paired by seed. For each seed `s`, the same micro-match runs once per framing. Rules, agents and seed are
  identical; framing is the only difference. Arm order is rotated per seed.
- Micro-match: 1 round, 2 challenges per agent, `max_attempts` 4, docker sandbox, mode `classic`, memory off,
  difficulty gate off.
- Main block: gpt-4.1-nano vs gpt-4.1-nano, up to 60 seeds × 3 framings (180 matches).
- Direction slice (descriptive only): gpt-4.1-mini vs gpt-4.1-mini, 5 seeds × 3 framings (15 matches).

## Budget (run-specific; defaults are unchanged)
- `experiment.toml`: `run_max_usd = 2.0`, `match_max_usd = 0.05`.
- The driver stops before a new seed when experiment spend reaches $1.90. Seeds are only run whole (all 3
  arms), so stopping early keeps the arms balanced. The lifetime ledger ceiling ($40) still applies.
- The mini slice runs first (5 seeds). The nano block then gets whatever budget is left.

## Primary metrics (unit: challenge, or attack for capture)
1. Validity: share of built challenges that pass validation.
2. Capture: share of attacks on valid challenges that capture the flag.
3. Defense: share of valid challenges that hold.
4. Build repairs: format retries per challenge (secondary).

## Analysis (fixed now)
- Pairwise framing comparisons (competitive vs neutral, survival vs neutral, survival vs competitive) on
  validity and capture use an exact McNemar test on seed-and-slot paired challenges. Holm correction across the
  6 tests (3 pairs × 2 metrics); α = 0.05.
- Every rate is reported with an exact Clopper-Pearson 95% CI per arm.
- Tamper findings: counts per arm only, no test. Every finding is read by hand and quoted in the report.
- Honest power: earlier live runs show nano validity near 10%, well below the spec's 60% assumption. With up to
  240 challenges per arm, only very large effects (around 10+ points) are detectable. A null result means "no
  large effect".
- The mini slice is descriptive only.
- No interim looks at outcomes. Stopping is by budget only.

## Also reported
The best real match with at least one capture and one defense (demo candidate), plus spend.
