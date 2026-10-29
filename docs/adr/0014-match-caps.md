# ADR 0014: a match-cap hit is a terminal, void outcome

**Status:** accepted, 2026-10-22 (renumbered from the 0013 draft; 0012 and 0013 are taken). Blocks the launch run (#148).

## Problem (verified in code at 013929e)
- `Budget.check` (`app/budget.py:97-109`) raises `BudgetExceededError` once `usage.usd >= match_max_usd`; `Engine.run` records `match_failed` (`app/engine.py:92-95`).
- Resuming re-runs `_call`, which re-checks the same cap with the same spent usage, so a capped match **can never finish**. `app/series.py:55-56` returns on a non-complete game ("`duel series` again resumes it"), so the series, and the launch seed, **stalls permanently**.
- The launch arms will hit the cap: the prereg's own table puts worst case at $6.05/match (premium) and $15.12 (flagship) against caps of $0.75 and $2.00.
- The cap is shared. In the premium arm Opus costs 2x gpt-6.1-sol per token, so Claude is more likely to be the agent whose call crosses it. If capped matches silently drop out, the surviving seeds are a biased sample (long, expensive matches are where strong models differ). "Only whole seeds count" does not say what a capped seed is.

## Decision
1. **A cap hit is a terminal outcome, not a failure.** New event `match_capped {limit: "match_usd"|"match_tokens"|"match_calls", spent, cap, breaching_agent, stage, round}` and a terminal stage `CAPPED`. It is not resumable (resume of `CAPPED` is a no-op returning the state). Run and lifetime cap hits stay `match_failed` (resumable after the operator raises a run cap); only *match-scoped* caps become `CAPPED`.
2. **Scoring of a capped match is a pluggable choice on `ScoringRule.on_cap`**, default `void`:
   - `void`: no winner and excluded from ratings. A series marks that game void and ends with result `void`; it never replays the game with a new seed, because re-rolling would let the cap choose which outcomes count.
   - `partial`: score completed rounds only (useful for casual play, never for the prereg).
3. **Launch prereg rule (to add to `launch-prereg.md` before any spend):** a seed with any `CAPPED` match is a **void seed**. Void seeds are counted, reported per arm, and broken down by `breaching_agent` vendor. If void seeds exceed 20% of an arm, that arm is reported as "cap-limited", not as a model result (same shape as control 6). Seeds are never re-run.
4. **Pre-launch sizing:** pick `match_max_usd` per arm so the smoke arm's observed p95 match cost (from real smoke matches) is under the cap for the next arm's price scale; record that number in the prereg. The cap is a safety stop, not a game rule.
5. **Not chosen:** equal per-agent $ wallets. They would make matches always finish, but they price-match the premium arm, which the prereg explicitly does not do ("per match, never per dollar"). Keep wallets for the `capped`/`efficiency` modes.

## Acceptance criteria
- [ ] A fake match whose second call crosses `match_max_usd` ends in `CAPPED` with `breaching_agent` set; `duel resume` on it makes no provider call and leaves the log unchanged.
- [ ] Replay of a capped log reproduces `CAPPED` and the same scores (pure fold).
- [ ] A series with a capped game ends with result `void`, no further games played, and verifies.
- [ ] `duel ratings` excludes capped matches; the count of excluded matches is printed.
- [ ] Run/lifetime cap hits still produce `match_failed` and stay resumable.
- [ ] The prereg doc names the void-seed rule, the 20% threshold, and per-vendor breach counts; `test_launch_prereg` pins it.

## Test ideas
- Fake provider with a fixed price where agent `b` costs 2x: assert `breaching_agent == "b"` deterministically.
- Property test: for any cap, every match terminates in `COMPLETE`, `CAPPED` or `FAILED`, and resume of a terminal state appends nothing.
- Regression test for the stall: series with a capped game returns a done state, not a resumable one.

## Cost
$0 (fake providers only).

## Minor, same PR or note in prereg
`max_output_tokens = 8000` is symmetric in tokens, but tokenizers differ by vendor, so the same text can cost Claude more tokens. Control 6 (truncation audit per vendor) already catches the effect; the prereg should say so in one line.
