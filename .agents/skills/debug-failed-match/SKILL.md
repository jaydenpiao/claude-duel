---
name: debug-failed-match
description: Use when a match ended in stage failed, is locked, or replay reports inconsistent.
---
1. Inspect `.duel/<id>.json` (or `DUEL_STORE_DIR`): `stage`, `failed_stage`, `failure`, last `events`. Don't paste `secret`.
2. `duel replay <id> --transcript` shows the event log; exit 1 means recomputed scores differ from recorded ones (scoring change or tampering).
3. `duel resume <id>` (or `run --match-id <id>`) calls `recover()` and continues from `failed_stage`; finished units are skipped.
4. `MatchLockedError`: a `<id>.lock` exists. Only delete it after confirming the PID inside isn't running.
5. `BudgetExceededError`: a cap was hit; raise the cap only with user approval, never edit `ledger.jsonl`.
6. More logs: `DUEL_LOG_LEVEL=DEBUG`. Reproduce the failure in a test before fixing.
