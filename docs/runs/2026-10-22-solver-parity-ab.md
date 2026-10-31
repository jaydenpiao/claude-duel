# Attacker solver parity A/B (#150)

Small smoke A/B (claude-haiku-5-5 vs gpt-6-luna) of `attacker_solver` with `integrity_control` on in both arms.
Setup: 6 paired seeds (`ab0`..`ab5`), seats alternated, checked mode, 3 rounds × 1 challenge, `max_attempts = 3`,
`max_output_tokens = 8000`, `reasoning_effort = "low"`, variety on, neutral framing, docker sandbox,
`match_max_usd = run_max_usd = 0.08` per match, A/B stop at $0.90. Total spent **$0.1945** (ledger lifetime now $2.43).

| arm | challenges | valid | captures | attempts | invalid replies | $ | haiku pts | luna pts |
|---|---|---|---|---|---|---|---|---|
| solver off | 36 | 36 | 33 (92%) | 51 | 18 | 0.138 | 708 | 786 |
| solver on | 36 | 35 | 35 (100% of valid) | 40 | 10 | 0.056 | 669 | 701 |

With the solver on, attackers used it on 20 of 40 attempts.

- **Integrity control:** it ran 88 times across both arms. The control flag was never captured and there were no
  `control-capture` findings, so nothing shows a sandbox or flag leak.
- **Solver parity:** with the solver on, attempts fell from 51 to 40, invalid replies from 18 to 10, and spend by 59%.
  The capture rate went from 92% to 100% of valid challenges. At this tier both models capture almost everything
  either way, so the solver mostly saves calls rather than changing outcomes.
- The defence-held count is already near zero at haiku/luna, so this A/B cannot show whether the solver changes who
  wins. That question belongs to the launch arms.
- With N=6 this is descriptive only and carries no significance claim.
