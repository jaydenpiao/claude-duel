# Benchmark slice (#45)

Goal: put a fixed external yardstick next to duel scores, so that a high capture rate can be read as
"the model is strong" or "the puzzles are easy". Product proposal 3 in
[product-2026-10-22.md](product-2026-10-22.md).

## Slice
- **Source:** stdin-only tasks from InterCode-CTF and NYU CTF Bench, using the patched copies in
  [Cyber-Zero's benchmarks](https://github.com/amazon-science/Cyber-Zero/blob/main/benchmarks/README.md).
  Use only tasks that fit the arena: one Python program that reads stdin, needs no network, server,
  shell or files, and has a single answer.
- **Size:** 10-20 tasks, chosen once and frozen by id, so solve rates stay comparable over time.
- **Port:** each task is rewritten into the arena form, a program that prints `FLAG` when the right
  stdin arrives (the arena injects `FLAG`, just as in a match). The original flag is never used.
- **Licensing:** the content belongs to the original authors. **Never vendor tasks into this repo.**
  Keep the manifest in a local, untracked directory (for example `~/duel-bench/tasks.jsonl`), and
  record `origin` and `license` on every task.
- **Contamination:** public tasks may be in training data. Report bench numbers as an anchor, not
  as ground truth.

## Manifest
One JSON object per line:

```json
{"id": "ic-ctf-17", "title": "...", "description": "...", "source": "import sys\n...", "origin": "<url>", "license": "<spdx or note>"}
```

## Harness (`app/bench.py`, `duel bench <manifest> [--tries N] [--format json]`)
- Each configured agent attacks every task, using the same `attack_prompt`, `parse_attack` and
  sandbox as a match, so bench and duel numbers measure the same skill. Each solve gets a fresh flag.
- The output is the per-task result (solved, tries, tokens) and each agent's solve rate.
- **Budget (#104):** every call goes through the match `CostBudget` via `MeteredProvider`, so the
  match caps (calls, tokens, USD) apply to the whole bench run, alongside the run caps and the $40
  lifetime ceiling. Every call is recorded in the shared `ledger.jsonl` under `bench-<id>`. Real
  models only run in Docker, because `make_sandbox` fails closed for them. Set per-run caps in a
  config, for example `[budget] match_max_usd = 0.10`. Fake agents solve only the template tasks the
  tests use.

## Before a full live run
- Pick and port the tasks locally, with a cap of about $5 for the whole slice. Record the ids, the
  models and the solve rates in `docs/runs`.
