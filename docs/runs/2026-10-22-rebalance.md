# Rebalance batch (#95), 2026-10-22

Checked mode with variety, `gate_mode = "score"`, and the #98 rebalance
(2 extra attack tries early on, retry hint). 3 rounds, cap $0.25, spend $0.2092.
rm = gpt-4.1-mini vs mini, rk = nano vs mini.

| Match | Valid | Captures a/b | Defenses a/b | Both |
|---|---|---|---|---|
| rm-1 | 2/6 | 0/0 | 2/0 | |
| rm-2 | 3/6 | 0/0 | 1/2 | |
| rm-3 | 3/6 | 0/0 | 1/2 | |
| rm-4 | 0/5 | 0/0 | 0/0 | |
| rm-5 | 6/6 | 1/1 | 2/2 | yes |
| rm-6 | 5/6 | 0/0 | 3/2 | |
| rk-1 | 3/6 | 0/1 | 0/2 | |
| rk-2 | 5/6 | 0/0 | 2/3 | |
| rk-3 | 3/6 | 1/0 | 1/1 | |
| rk-4 | 3/5 | 0/1 | 1/1 | |

Best match: **rm-5**. All 6 checks were valid, the designs varied (state
machine maze, XOR shift maze, modular maze, weighted checksum, stack VM),
and both sides captured and defended. The replay is consistent and the
score was 210-120.

Gap: rm-5's captures both landed on the first try. The only multi-try
capture was in rk-1 (bob, 2 tries), where just one side captured. No match
in this batch met every demo criterion.
