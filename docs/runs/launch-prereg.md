# Pre-registration: strongest Claude vs strongest GPT (launch)

Written before any launch spend (#148). Nothing here has been run on real models except the smokes in
[2026-10-22-cross-vendor-smoke.md](2026-10-22-cross-vendor-smoke.md).

## Question
In a symmetric staged CTF (each side builds a challenge the arena proves solvable, then attacks the other's),
does the strongest Anthropic model beat the strongest OpenAI model on captures and defenses?

## Arms (chosen by the owner before any launch spend; not changed after)
Pinned in [launch-arms.toml](launch-arms.toml). N counts **paired seeds**: each seed is played twice with the
seats swapped, so an arm is 2N matches. Arms run in this order, each only after the previous one finished
and replay-verified:

| order | arm | Anthropic | OpenAI | price $/M in/out (A / O) | N | `run_max_usd` | `match_max_usd` |
|---|---|---|---|---|---|---|---|
| 1 | smoke (pipeline check) | claude-haiku-5-5 | gpt-6-luna | 0.10/0.50 / 0.10/0.50 | 100 | 3 | 0.15 |
| 2 | premium | claude-opus-5-5 | gpt-6.1-sol | 4/20 / 2/10 | 10 | 10 | 0.85 |
| 3 | flagship | claude-fable-5-1 | gpt-6-astra | 10/50 / 10/50 | 5 | 17.5 | 2.75 |

Total cap $30.5, within what is left of the $33 lifetime ledger after about $2.3 already spent ($30.7). The smoke arm goes first because it is
cheap enough to show that the pipeline works end to end (valid builds, the control, replay, the cost lines)
before any expensive call.

**The premium arm is not price-matched.** Opus costs twice what gpt-6.1-sol does per token, so its result
is reported with **$/win** for each vendor (spend divided by matches won) beside the win share, and its
headline is "per match", never "per dollar". The smoke and flagship arms are price-matched.

**N fits the cap at typical cost** (owner's choice: lower N, never raise a cap; the flagship arm was cut to N=5 and $17.5 so all three arms fit the ~$30.7 left). Typical cost per seed is
two matches (cost table below): smoke $0.028, premium $0.828, flagship $2.76. Each N is the largest whole
number of seeds whose typical cost leaves at least 15% of the cap as headroom:

| arm | N seeds | typical $ | cap | headroom |
|---|---|---|---|---|
| smoke | 100 | 2.76 | 3 | 8% (N=100 kept by the owner) |
| premium | 10 | 8.28 | 10 | 17% (N=11 would leave 9%) |
| flagship | 5 | 13.80 | 17.5 | 21% (N=6 would leave 5%) |

**Per-match caps** (`match_max_usd`) are a safety stop, not a game rule. Each is about 2× the arm's
typical match: smoke 0.15 (its worst case), premium 0.85 (2 × $0.414), flagship 2.75 (2 × $1.38). An arm's
headroom over typical cost (smoke $0.24, premium $1.72, flagship $3.70) therefore absorbs at least one capped
match without ending the arm early. Before the premium arm, the smoke arm's observed p95 match cost is
scaled by the price ratio and recorded here. If that figure exceeds the premium or flagship cap, the cap is
raised before any premium spend, and never after any of that arm's seeds have been seen.

Seeds run in a fixed order (`launch-0`, `launch-1`, ...) and only whole seeds count. If a worse-than-typical
run hits `run_max_usd` early, N shrinks without anyone choosing which seeds count. The N reached is reported.

### Void seeds (match cap hits, ADR "match caps")
A match that crosses `match_max_usd` ends in the terminal stage `CAPPED`. It records the breaching agent, is
never resumed, and counts as void (no winner, excluded from ratings). A seed with any `CAPPED` match is a
**void seed**:
- Void seeds are never re-run or replaced by a new seed. Re-rolling would let the cap choose which outcomes count.
- Void seeds are excluded from the sign test, counted, and reported per arm, broken down by the vendor of the
  breaching agent. In the premium arm, Opus costs 2× per token, so it is more likely to be the breacher.
- If **more than 20%** of an arm's seeds are void, that arm is reported as **cap-limited**: a statement about
  the caps, not a model result (same shape as control 6).
- `max_output_tokens = 8000` is equal in tokens, but tokenizers differ, so the same text can cost one vendor
  more tokens. Control 6 (the per-vendor truncation audit) catches this effect.

## Pinned rules (identical for both vendors)
- `mode = "checked"`, `rounds = 3`, `challenges_per_round = 1`, `max_attempts = 3`, docker sandbox.
- `max_output_tokens = 8000` (reasoning included) and `reasoning_effort = "low"` for both sides: Anthropic
  `output_config.effort`, OpenAI Responses `reasoning.effort`. No temperature (unsupported on gpt-6).
- Both sides use the same strict JSON schema per purpose (#145). A reply cut at the cap gets one repair call;
  a second cut is the failure `truncated`.
- `variety = true`. The smoke showed an author copying the prompt's example challenge.
- Same system prompts, same framing (`neutral`) and no handicaps.

## Controls (why a result is not fake)
1. **Neutral labels and seat swap.** Agents are named `a`/`b`, never by vendor. Each seed is played twice,
   once with Claude as `a` and once as `b`. The dry run below shows why: with identical fake agents, the
   winner followed the *name* (the fake hashes it), not the seat. With the seats swapped, the same name still won.
   Model names (#219, #229) are display labels only: the arena, commentary, card and replay output show them,
   but agent ids stay `a`/`b` and no prompt or system prompt names a model or vendor (pinned by
   `tests/integration/test_neutral_labels.py`). Clarified 2026-10-31, before any spend.
2. **Replay verification.** Every match log must replay to the same state (`series verified ✓`). A mismatch
   voids the match.
3. **Empty-challenge integrity control** (required before launch, ~$0; `integrity_control = true` in launch.toml). A challenge that can never print the
   flag is planted. Any capture against it proves an arena leak and voids the run.
4. **Attacker solver parity** (required before launch; `attacker_solver = true` in launch.toml). Attackers may run a sandboxed solver with a throwaway
   flag, and its stdout becomes the payload. An A/B with and without the solver, capped at $1, runs on the
   smoke tier first. Without parity, strong vs strong can end with no captures and no signal.
5. **Symmetric accounting.** Each agent's tokens in, out and thinking, plus $, $/capture and $/defense, are
   shown live and on the card (#146). All failed and truncated calls count.
6. **Failure audit.** Every `malformed`, `truncated` or rejected build is counted per vendor and reported. If
   one vendor fails for format reasons above 20% of builds, the result is reported as a harness result, not
   a model result.

## Primary metrics and analysis (fixed now)
- Unit: a paired seed (both seat orders). Claude **wins the seed** if it wins both matches, loses it if it
  loses both. A split is a tie.
- Primary, per arm: Claude's share of decided seeds. Two-sided exact sign test with ties dropped.
- **Holm correction across the three arms** (family α = 0.05): sort the three p-values; the smallest must
  be ≤ 0.0167, the next ≤ 0.025 and the last ≤ 0.05, stopping at the first that fails. The smoke arm is in
  the family even though it is a pipeline check, so it cannot be dropped after the fact.
- Secondary: capture and defense rate per vendor with Clopper-Pearson 95% CIs; Bradley-Terry with bootstrap
  CIs from `duel ratings`; $/capture, $/defense and (premium) $/win per vendor.
- No interim looks at outcomes. Stopping is by budget only, and only whole seeds count.

### Power (exact sign test, assuming no ties; ties lower it further)
At the strictest Holm level, α = 0.0167:

| arm | N seeds | win share needed to reject (all of N decided) | power if true share 0.6 / 0.7 / 0.8 / 0.9 | 80% power needs a true share of |
|---|---|---|---|---|
| smoke | 100 | ≥ 63/100 | 0.31 / 0.95 / 1.00 / 1.00 | 0.67 |
| premium | 10 | 10/10 | 0.01 / 0.03 / 0.11 / 0.35 | 0.98 |
| flagship | 5 | cannot reject (smallest two-sided p = 2/32 = 0.0625) | 0 / 0 / 0 / 0 | — |

At α = 0.05 the 80%-power shares are 0.65 (smoke), 0.92 (premium, needs ≥ 9/10) and none for flagship (N=5 can never reach
p < 0.05). What this means: only the smoke arm is a real test, and it can detect a moderate edge. **Premium and
flagship are descriptive.** Premium rejects only on a clean sweep. Flagship cannot reject at any Holm
level, even on a 5/5 sweep.
Both are reported with win share, exact 95% CI and $/win, never as a headline significance test. A null
result there means "no evidence", not "no edge".

## Cost table
Per match: 3 rounds × 2 agents. **Typical** is 12 calls at 1,500 in / 2,000 out tokens (smoke match
`21f0365f9b49`: builds 700–1,800 in / 900–2,800 out, of which 670–2,100 were thinking). **Worst** is 36 calls
(1 build + 2 format retries + 3 attempts per challenge) at 2,000 in / 8,000 out, the cap. N = matches
(a seed with both seat orders is 2 matches).

| tier | typical $/match | worst $/match | N=10 typ (worst) | N=30 typ (worst) | N=100 typ (worst) |
|---|---|---|---|---|---|
| haiku-5-5 vs gpt-6-luna | $0.014 | $0.151 | $0.14 ($1.51) | $0.41 ($4.54) | $1.38 ($15.12) |
| sonnet-5-5 vs gpt-6.1-sol | $0.276 | $3.024 | $2.76 ($30.24) | $8.28 ($90.72) | $27.60 ($302.40) |
| opus-5-5 vs gpt-5.6-sol | $0.552 | $6.048 | $5.52 ($60.48) | $16.56 ($181.44) | $55.20 ($604.80) |
| fable-5-1 vs gpt-6-astra | $1.380 | $15.120 | $13.80 ($151.20) | $41.40 ($453.60) | $138.00 ($1,512.00) |

Budget: the lifetime ledger ceiling is $33 and about $30.7 remains (~$2.3 spent). Worst cases above that are stopped by
run-specific caps (`run_max_usd`, `match_max_usd`), never by raising defaults. The owner's arms and caps are under "Arms" above.
Premium (opus $4/$20 vs gpt-6.1-sol $2/$10, not in the table above) is about $0.41 typical and $4.54 worst per match.

## Fake dry run (done, $0)
`duel series --games 5` with the pinned rules on fake agents, docker sandbox: 3 games played (best-of-5
decided), each 6/6 valid challenges, 3 captures and 3 defenses, `series verified ✓`, $0.0000. With the seats swapped the same
name won again (gpt 117, claude 0 in every game). Identical agents, opposite fortunes by label: that is the
confound control 1 removes. Config: [launch.toml](launch.toml) (fake agents; set `provider`/`model` for real).
