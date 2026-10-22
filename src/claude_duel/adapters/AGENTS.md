# adapters

Implementations of the Protocols in `ports.py`. Chosen only in `app/container.py`. Root: [AGENTS.md](../../../AGENTS.md).

## providers/
- `fake.py`: deterministic offline opponent; `skill` controls difficulty. Default for tests, CI and staging.
- `openai.py`: httpx client; `raise_for_status` maps HTTP errors to domain errors (quota vs retryable, `Retry-After`).
- `retry.py`: `RetryingProvider` wraps any provider with jittered backoff (`backoff_delay`); quota errors are not retried.
- Extend: [add-provider skill](../../../.agents/skills/add-provider/SKILL.md).

## sandbox/
- `common.py:execute`: the single process runner (own process group, wall timeout, streamed output cap, group kill + `cleanup` after every run). Reuse it; don't fork it.
- `runner.py`: in-sandbox loader; flag, source and rlimits go over stdin (`frame`), never argv/env.
- `subprocess_sandbox.py`: rlimits (CPU, AS, FSIZE=0, NOFILE=16, NPROC=1), `python -I -S`, read-only temp dir, minimal env. Fake provider only; not a hard boundary.
- `docker_sandbox.py`: hardened `docker run` (see ADR 0003), limits from `SandboxLimits`, `docker rm -f` after every run. `docker_probe` gates it; real-model matches fail closed without it. Tests: `tests/integration/test_docker_sandbox.py`.
- Never pass the host env through; the child env is only a minimal PATH.

## store/
- `file_store.py`: atomic JSON save (tmp + fsync + rename, mode 0600), non-blocking `flock` on `<id>.lock` (`MatchLockedError`); the kernel releases it if the holder dies, so locks never go stale. Never unlink lock files. Match ids must be alphanumeric/hyphen.
- `jsonl.py`: shared flock + fsync append-only JSON-lines helpers used by the ledger and event log.
- `event_log.py`: per-match hash-chained `<id>.events.jsonl`; reads verify the chain (`EventLogCorruptError`).
- `ObservedEventLog` (in `event_log.py`): decorator that calls an `EventObserver` after each durable append.
- `ledger.py`: append-only `ledger.jsonl`; every read and append holds `flock`, appends fsync, readers fold only new bytes, a torn last line is skipped and never glued to the next entry. Every call is recorded, $0 included. Totals feed the $40 lifetime cap across processes. Don't delete it to "reset" spend.
