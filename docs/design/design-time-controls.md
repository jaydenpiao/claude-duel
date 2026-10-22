# Design: time controls and time-scored modes (issue #13 "add call and match deadlines")
Depends on: design-rules-abstraction.md (`Rules.clock`, scorer `timed`).

> **Lead decisions (2026-10-22, 1:08 AM PDT), now binding:** game clocks are measured in tokens, and real latency is recorded only as a metric; cross-vendor efficiency is scored in dollars, with tokens shown alongside; the default match deadline is 900 s; per-agent wallets sit under the existing cost caps and the strictest limit wins; the build order is approved and starts after ADR 0006 merges.

## Research basis
- Fischer increment (time added after each move, unused time carries over) and Bronstein delay (adds back only time actually used, up to the delay): https://en.wikipedia.org/wiki/Time_control
- FIDE Laws of Chess, Appendices A/B (rapid/blitz defined using "allotted time plus 60 times any increment"): https://handbook.fide.com/chapter/E012023
- CTF tie-breaking by time of solve (CTFtime overview): https://www.ctftime.org/ctf-wtf/

## Two layers
**A. Safety deadlines (#13, always on, not scored).**
- `Clock.call_deadline_s` (default = `Settings.provider_timeout`, 60 s): the total time for one logical call **including retries**. `RetryingProvider` gets a `deadline` and fails fast when `now + backoff > deadline` or when `retry_after > remaining` (that's the issue's ask). It raises `DeadlineExceededError` (a resumable stage failure, not a forfeit).
- `Clock.match_deadline_s` (**default 900 in every env**, decided): the summed recorded `elapsed_ms` across `call_charged` events. When exceeded, the match goes to `failed` with reason `deadline`; `resume` gets a fresh process window but the **recorded** total keeps counting, so it can't be gamed by restarting.

**B. Game clock (opt-in mode `blitz`, scored).** Each agent has a bank `bank_tokens` (e.g. 40k output-token-equivalents) for its own provider calls across the match, plus `increment_tokens` per completed call (Fischer) or a Bronstein delay. **Clock time = tokens (decided)**: each call charges `completion.output_tokens + input_tokens // clock.input_divisor` against the caller's bank, read from the recorded `call_charged` event. Real latency (`elapsed_ms`) is recorded **only as a metric**. It feeds the safety deadlines in layer A and the dashboards, never the clock or the score. Sandbox time is the arena's and is not charged.
- Flag fall: an agent whose bank is <= 0 forfeits its remaining builds in that round (`forfeit_reason="clock"`) and stops attacking (`Attack.reason="clock"`).
- Timed scoring (`scoring.kind="timed"`): a capture is worth `solve_award - slow_penalty`, where `slow_penalty = min(solve_award - 1, floor(timed.max_penalty * min(1, attack_clock_tokens / timed.par_tokens)))`. That is a discount, never a bonus, because ADR 0006 makes defense == capture (10/10). Any bonus above `solve_award` would break its defense >= capture property.

## Determinism and fairness
- Latency is non-deterministic, so it is **measured once, recorded, and never re-measured**. The clock state is a fold over `call_charged` events, and replay reproduces flag falls exactly.
- Provider latency depends on network and vendor load and isn't skill. That's why **the game clock is token-based only (decided; no wall-clock option)**: it is fully deterministic and replays exactly. Tokens are not tokenizer-neutral across vendors, so a cross-vendor blitz should use a handicap (`bank_mult`) calibrated from same-task token counts. Wall time is used only for the safety deadlines.
- The fake provider gets a deterministic `latency_ms` derived from the seed, for tests.
- Alternate agents' call order each round (A first in odd rounds) so rate-limit queueing doesn't bias one side.

## Built (#36)
- `rules.clock` (`ClockRule`) folded in `app/allowance.py` alongside wallets; `call_charged` now records `input_tokens`/`output_tokens`; `timed` scorer uses the attack's recorded `tokens_used` as its clock tokens.
- Not yet: Bronstein delay, `bank_mult` handicaps (see #38), alternating call order.

## Built (#67)
- Scored presets run on token clocks: `blitz` (40k + 500) and `efficiency` (60k + 500). `capped` is bounded by its token wallet. A test runs each scored preset with and without injected provider latency and requires identical outcomes and clock usage. The 900 s wall deadline only fails a match.

## Fit
- `app/engine.py::_call` already times calls (`time.monotonic`) for metrics. Pass that value into the `call_charged` event (additive field, default 0 for old logs).
- New `app/clock.py::MatchClock` (pure fold + check), wired in `container.py`.
- `retry.py`: deadline parameter; the existing `RateLimitError.retry_after` handling is the place for the fail-fast.

## Cost
Safety deadlines: $0. Blitz reduces spend because banks cut long attack loops. A 20-match tuning sweep at <= $0.03 is <= $0.60.

## Acceptance criteria
- [ ] A retry whose `retry_after` exceeds the remaining deadline raises immediately (no sleep); tested with a fake clock.
- [ ] A match over `match_deadline_s` fails with `deadline`, and resume doesn't reset the recorded total.
- [ ] Blitz: replaying a log reproduces the same flag-fall point and score.
- [ ] The token clock gives identical results across two runs of the same seeded fake match; changing fake latency changes no score and no flag fall.
- [ ] The timed scorer passes the shared scorer invariants (the 0006 inequality).

## Test ideas
- Injected monotonic clock in `RetryingProvider` (no real sleeps).
- Event-log fixture with crafted token counts that trigger a flag fall mid-attack; a fixture with huge `elapsed_ms` only trips the 900 s safety deadline, never the game clock.
- Property: the capture value is monotone non-increasing in clock tokens and never breaks the defense >= capture rule.
