# adapters

Implementations of the Protocols in `ports.py`. Chosen only in `app/container.py`. Root: [AGENTS.md](../../../AGENTS.md).

## providers/
- `fake.py`: deterministic offline opponent; `skill` controls difficulty. Default for tests, CI and staging.
- `openai.py`: httpx client; `raise_for_status` maps HTTP errors to domain errors (quota vs retryable, `Retry-After`).
- `retry.py`: `RetryingProvider` wraps any provider with jittered backoff (`backoff_delay`); quota errors are not retried.
- Extend: [add-provider skill](../../../.agents/skills/add-provider/SKILL.md).

## sandbox/
- `common.py:execute`: the single subprocess runner (wall timeout, output cap). Reuse it; don't fork it.
- `subprocess_sandbox.py`: rlimits (CPU, AS, FSIZE=0, NOFILE=16), `python -I -S`, read-only temp dir, minimal env. Not a hard boundary.
- `docker_sandbox.py`: `docker run` with no network, read-only, cap-drop ALL, no-new-privileges, pids 32, user 65534, `python:3.12-slim`. Excluded from coverage. `sandbox="auto"` uses it when `docker_available()`.
- Never pass the host env through; only `{"FLAG": ...}` plus a minimal PATH.

## store/
- `file_store.py`: atomic JSON save (tmp + fsync + rename, mode 0600), `O_EXCL` lock files (`MatchLockedError`). Match ids must be alphanumeric/hyphen.
- `ledger.py`: append-only `ledger.jsonl` with fsync; torn last lines are skipped. Totals feed the $40 lifetime cap. Don't delete it to "reset" spend.
