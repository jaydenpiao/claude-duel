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
| 2 | premium | claude-opus-5-5 | gpt-6.1-sol | 4/20 / 2/10 | 20 | 10 | 0.75 |
| 3 | flagship | claude-fable-5-1 | gpt-6-astra | 10/50 / 10/50 | 10 | 20 | 2.0 |

Total cap $33, under the $40 lifetime ledger (about $37.7 left). The smoke arm goes first because it is
cheap enough to show that the pipeline works end to end (valid builds, the control, replay, the cost lines)
before any expensive call.

**The premium arm is not price-matched.** Opus costs twice what gpt-6.1-sol does per token, so its result
is reported with **$/win** for each vendor (spend divided by matches won) beside the win share, and its
headline is "per match", never "per dollar". The smoke and flagship arms are price-matched.

**Caps bind before N at typical cost** (cost table below). Premium is about $0.41 per match, so $10 covers
about 12 of the 20 seeds. Flagship is about $1.38 per match, so $20 covers about 7 of the 10 seeds; its
`match_max_usd` is 2.0 because 0.75 would cut most flagship matches short. Seeds run in a fixed order
(`launch-0`, `launch-1`, ...) and only whole seeds count, so a cap ending an arm early reduces N without
choosing which seeds count. The N reached is reported.

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
2. **Replay verification.** Every match log must replay to the same state (`series verified ✓`). A mismatch
   voids the match.
3. **Empty-challenge integrity control** (required before launch, ~$0). A challenge that can never print the
   flag is planted. Any capture against it proves an arena leak and voids the run.
4. **Attacker solver parity** (required before launch). Attackers may run a sandboxed solver with a throwaway
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
| premium | 20 (about 12 within cap) | ≥ 16/20 | 0.05 / 0.24 / 0.63 / 0.96 | 0.85 |
| flagship | 10 (about 7 within cap) | 10/10 | 0.01 / 0.03 / 0.11 / 0.35 | 0.98 |

At α = 0.05 the 80%-power shares are 0.65, 0.80 and 0.92. What this means: the smoke arm can detect a
moderate edge. Premium can detect only a large one. Flagship is **descriptive**: only a clean sweep rejects
the null, so a null flagship result means "no evidence", not "no edge". It is reported with its CI and is
not used as the headline test.

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

Budget: the lifetime ledger ceiling is $40 and about $37.7 remains. Worst cases above that are stopped by
run-specific caps (`run_max_usd`, `match_max_usd`), never by raising defaults. The owner's arms and caps are under "Arms" above.
Premium (opus $4/$20 vs gpt-6.1-sol $2/$10, not in the table above) is about $0.41 typical and $4.54 worst per match.

## Fake dry run (done, $0)
`duel series --games 5` with the pinned rules on fake agents, docker sandbox: 3 games played (best-of-5
decided), each 6/6 valid challenges, 3 captures and 3 defenses, `series verified ✓`, $0.0000. With the seats swapped the same
name won again (gpt 117, claude 0 in every game). Identical agents, opposite fortunes by label: that is the
confound control 1 removes. Config: [launch.toml](launch.toml) (fake agents; set `provider`/`model` for real).
