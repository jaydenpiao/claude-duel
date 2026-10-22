# Design: token-efficiency and cost-capped duels
Depends on: design-rules-abstraction.md (`Rules.token_cap`, scorer `efficiency`).

> **Lead decisions (2026-10-22, 1:08 AM PDT), now binding:** game clocks are measured in tokens, and real latency is recorded only as a metric; cross-vendor efficiency is scored in dollars, with tokens shown alongside; the default match deadline is 900 s; per-agent wallets sit under the existing cost caps and the strictest limit wins; the build order is approved and starts after ADR 0006 merges.

## Research basis
- LLM arenas compare models under fixed conditions and report cost alongside strength. Chatbot Arena paper: https://arxiv.org/abs/2403.04132
- Current per-token prices used for estimates: https://developers.openai.com/api/docs/pricing (repo `DEFAULT_PRICES` is the source of truth)

## Modes
1. **`efficiency` (token-scored).** A capture is worth `solve_award - floor(e.max_penalty * spent / e.reference_spend)` (min 1), where `tokens_used` is the already-recorded `Attack.tokens_used`. It is a discount for waste, not a bonus, so ADR 0006's defense >= capture still holds with its 10/10 numbers. Builds cost nothing in scoring, but `build_tokens` is shown.
2. **`capped` (hard per-agent token or $ budget).** `TokenCap(per_agent_tokens=30_000)` or `per_agent_usd`. Each agent has a match-wide wallet spent on its own builds and attacks. An empty wallet means forfeiting builds and stopping attacks, the same mechanics as the blitz flag fall, sharing a single `Allowance` helper with the clock (one fold over `call_charged`, keyed on agent; **no duplicate code**). **Cross-vendor scoring is in dollars (decided)**: the discount uses the recorded `usd` (stored as integer micro-dollars, `round(usd*1e6)`, so scores stay integer and replay-exact), and commentary/JSON show tokens next to the $. For same-model duels, $ and tokens rank identically. Prices are pinned in `match_created` (the resolved price row per model), so a later price-table change can't alter a replayed score.
   **Wallets sit under the existing caps; the strictest limit wins (decided).** Before each call the engine checks, in order, the agent wallet, `match_max_*`, `run_max_*`, and lifetime, and refuses on the first that is hit. The wallet only ever tightens, and `BudgetLimits` remains the safety floor. A config whose wallets sum above `match_max_usd` is accepted but logs a warning that the match cap binds first.
3. **`max_tokens` per call.** Pass `CompletionRequest.max_output_tokens` (new optional field) so a verbose model can't burn the wallet in one call. Truncated output means a malformed answer, which is already handled.

## Determinism and fairness
- Tokens come from provider usage fields recorded in `call_charged.tokens` and `AttackAttempt.tokens`, so replay is exact.
- Tokenizers differ across vendors, so token counts aren't comparable. **Resolved: score in $ and display tokens.** `efficiency.unit = "usd"` (default) | `"tokens"` (same-vendor studies only).
- The discount must never make giving up better than capturing: a capture is always >= 1 point, and defense awards are unchanged.

## Built (#35, first PR)
- `capped` (token or $ wallets, strictest wins, `app/allowance.py`) and `efficiency` scored on recorded tokens (`scoring.efficiency`).
- Second PR: `ScoringRule.unit = "usd"` scores recorded `usd_micros` (on each `AttackAttempt`/`Attack`; the price is pinned by recording the charge rather than in `match_created`, so replay never re-prices), and `WalletRule.max_output_tokens` caps `CompletionRequest.max_tokens` (the existing field) per call. Default unit stays `tokens` so existing matches keep their meaning; no warning yet when wallets sum above `match_max_usd`.

## Fit
- Agent wallets live in the existing `Usage` model, per agent (`MatchState.usage_by_agent`, folded from `call_charged.agent`, which is already in the event).
- `CostBudget.check` remains the safety cap. The wallet is a game rule in `app/allowance.py`. They are separate on purpose (safety vs. scoring).

## Cost
These modes cap spend by construction. `capped` at 30k tokens/agent is about 60k tokens per match, ~$0.01 on gpt-4.1-nano. A 30-match study is about $0.30.

## Acceptance criteria
- [ ] An agent with an empty wallet makes no further calls (assert the fake provider call count).
- [ ] Strictest-wins: with wallet > match cap, the match cap stops the agent; with wallet < match cap, the wallet does, and the error names the limit hit.
- [x] Replay with a modified `DEFAULT_PRICES` still reproduces scores (pinned prices).
- [ ] The efficiency discount is computed only from recorded tokens; replay equals live.
- [ ] Scorer invariants hold (a capture is always >= 1).
- [ ] `duel estimate --mode capped` prints <= the configured cap x price.

## Test ideas
- Fake provider with configurable per-call token counts to hit the wallet boundary exactly (off-by-one: a call that *starts* under the cap is allowed, like `CostBudget.check`; document overshoot of <= 1 call).
- Hypothesis: the capture value is monotone non-increasing in tokens_used and >= 1.
