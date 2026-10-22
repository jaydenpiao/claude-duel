# Bench smoke (#104), 2026-10-22

`duel bench` with budget caps on, against 4 hand-written stdin tasks kept outside the repo (a Caesar
shift, a digit-sum puzzle, an XOR key and a tiny stack VM). gpt-4.1-nano against a fake agent,
3 tries per task, `match_max_usd = run_max_usd = 0.10`, Docker sandbox.

- Run id `bench-b90597e233f9`: 22 calls, 4,779 tokens, **$0.000284**. All 22 calls are in `ledger.jsonl`.
- Solve rate: nano 25% (caesar on try 1); fake 0%, as expected, since these aren't its templates.
- This only shows the budget path works live. It isn't a benchmark result.
