# Design priorities (ready-to-build order), 2026-10-22
**Status: build order APPROVED by lead (2026-10-22, 1:08 AM PDT); work starts once ADR 0006 merges.** Lead decisions folded into the specs: token-based game clocks (latency recorded as a metric only), cross-vendor efficiency scored in $ with tokens shown, a 900 s default match deadline, and wallets under the existing caps (strictest limit wins).

Ordering follows the lead's steer: mode abstraction → memory (#19) → time (#13) → token efficiency → formats. All of it is **after the `harder-challenges` work (ADR 0006) merges**, and on top of it.

1. **Land `harder-challenges` / ADR 0006 first** (not mine, prerequisite): every spec here inherits its scoring numbers (defense 10 >= solve 10), `match_stats` and the "scoring is a pure function of the log" invariant.
2. **ADR 0007 mode abstraction, step 1: `ScoringRule` + `SCORERS` registry with only `classic`** (design-rules-abstraction.md): a zero-behaviour-change refactor that every later mode plugs into; snapshot-provable.
3. **ADR 0007 step 2: presets (`MODES`), `rules_version`, `seed` and `elapsed_ms` in events**: cheap, additive, and it unlocks deterministic replay for time and memory.
4. **Between-round memory, `summary` kind (#19)** (design-memory-strategy.md): the biggest gameplay payoff (strategies evolve); pure, deterministic, ~$0.002/match.
5. **Safety deadlines: per-call deadline with retry fail-fast + 900 s default match deadline (#13 part A)** (design-time-controls.md): closes an open robustness issue, $0, small.
6. **Memory scratchpad (`summary+scratchpad`)**: small increment on 4, needs one new event.
7. **Token-efficiency mode + per-agent wallets (`efficiency` scored in $ with tokens shown, `capped`)** (design-efficiency-modes.md): shares the allowance fold with the clock; directly cuts spend; strictest of wallet/match/run/lifetime wins.
8. **Blitz game clock in tokens + `timed` scorer (#13 part B)**: needs 3 and 7's allowance helper; wall-clock time never scores (decided).
9. **`duel estimate --mode`**: an offline cost preview for every preset; guards the $50 key.
10. **Series (best-of-N) + handicaps** (design-match-formats.md): the first format layer, no state-machine changes.
11. **Ratings (BT + Elo) and round-robin/Swiss tournaments; CTFd-style decay scorer**: needs many matches; do it once modes are stable.


### Stakes track (design-stakes-modes.md), slotted in after item 4
4a. **FramingRule (neutral/competitive/survival) + equal-surface test + tamper detector with `finding_flagged`**: $0, makes framing a measurable A/B variable and adds security telemetry. **Recommended first stakes build.**
4b. **Pre-registered paired framing experiment (≤ $1.98, nano plus a small mini slice)**: answers whether stakes framing changes validity, capture or rule-breaking. It can detect only effects of roughly 12 points or more.
10a. **Deathmatch Bo1/Bo3 with elimination status**: after Series (10).
10b. **Survival task over a seeded challenge corpus under the token clock or wallet**: after 7, 8 and 10.
11a. **Ladder / battle royale / lives and revives**: with tournaments (11).

Estimated total API spend to build and validate items 2-11 (experiments at n≈20-30 per mode on gpt-4.1-nano): about $5, plus ≤ $2 for the stakes experiment, so ≈ $7 total, well under the $33 lifetime cap and $50 key.
