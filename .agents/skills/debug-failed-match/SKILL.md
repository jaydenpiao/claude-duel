---
name: debug-failed-match
description: Use when a match ended in stage failed, is locked, or replay reports inconsistent.
---
1. Inspect `.duel/<id>.json` (or `DUEL_STORE_DIR`): `stage`, `failed_stage`, `failure`, `last_seq`; the history is in `.duel/<id>.events.jsonl`. Don't paste `secret`.
2. `duel replay <id> --transcript` shows the event log; exit 1 lists `mismatches` (snapshot drift, rescored rounds after a scoring change); `EventLogCorruptError` means the log was edited.
3. `duel resume <id>` (or `run --match-id <id>`) calls `recover()` and continues from `failed_stage`; finished units are skipped. Resume uses the match's saved agents, models, rules and limits, not current config.
4. `MatchLockedError`: another live process holds the match's `flock` (its PID is in `<id>.lock`). A dead holder releases it automatically; never delete the lock file.
5. `BudgetExceededError`: a cap was hit; raise the cap only with user approval, never edit `ledger.jsonl`.
6. More logs: `DUEL_LOG_LEVEL=DEBUG`. Reproduce the failure in a test before fixing.
