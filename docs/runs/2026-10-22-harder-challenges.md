# Run report: harder challenges (2026-10-22)

All matches had 3 rounds with 1 challenge per side per round, 3 attack tries, up to 3 repair attempts, the Docker sandbox and a $0.05 cap per match. Costs come from the spend ledger. Each line in "Prompt/arena change" is the change made before that match.

| Match | Models (alice vs bob) | Prompt/arena change | Valid | Captures | Defenses | Cost |
|---|---|---|---|---|---|---|
| hard-1 | nano vs mini | hidden-answer policy, multi-step design prompt | 0/6 | 0 | 0 | $0.0104 |
| hard-2 | nano vs mini | + previous answer in repair, trace field | 1/6 | 1 (try 1) | 0 | $0.0237 |
| hard-3 | nano vs mini | + longer build output | 1/6 | 1 (try 2) | 0 | $0.0141 |
| hard-4 | nano vs mini | + actionable rejection hints | 1/6 | 0 | 1 | $0.0265 |
| hard-5 | nano vs mini | trace removed | 0/6 | 0 | 0 | $0.0099 |
| hard-6 | nano vs mini | + debug-print tip | 0/6 | 0 | 0 | $0.0153 |
| hard-7 | nano vs mini | maze-centred design | 0/6 | 0 | 0 | $0.0202 |
| hard-8 | nano vs mini | + solver field | 0/6 | 0 | 0 | $0.0206 |
| hard-9 | nano vs mini | solver = copied check + search | 1/6 | 0 | 1 | $0.0178 |
| hard-10 | nano vs mini | check(s) structure | 2/6 | 0 | 2 | $0.0196 |
| hard-11 | nano vs mini | + crash type/line, `sys.exit` and `_` allowed | 2/6 | 0 | 2 (one each) | $0.0189 |
| hard-mm-1 | mini vs mini | same as hard-11 | 1/6 | 0 | 1 | $0.0409 |
| hard-gate-1 | nano vs mini | + difficulty gate (nano probe) | 1/6 | 0 | 1 | $0.0264 |

The 13 matches cost $0.2643 in total. For comparison, the earlier tune-1..4 matches, before the hidden-answer rule, had 15/24 valid challenges and 15 captures out of 15 attacks, all on the first try, with 0 defenses.

**Findings.** Hidden answers and defense-first scoring do produce defenses, and both sides defended once in hard-11. Validity is what limits us now. Low-tier authors (and mini as well) usually write a reference payload or solver that their own challenge rejects. No match had a capture and a defense at the same time, so none of them is demo-worthy yet. The next lever (tracked as an issue) is for the arena to search for the answer itself: the author declares `check(s)`, an alphabet and a length, and the arena searches inside one sandbox run.
