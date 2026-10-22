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
- It runs **fake agents only** for now. Fake agents can only solve the template tasks the tests use,
  which is enough to exercise the harness.

## Before a live run (follow-up)
- Route the calls through the budget layer (per-run USD cap, ledger, model allowlist), with a cap of
  about $5 for the full slice as in proposal 3.
- Run tasks under the Docker sandbox only, the same as for real-model matches.
- Pick and port the tasks locally. Record the ids, the chosen models and the solve rates in `docs/runs`.
