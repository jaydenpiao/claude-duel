# adapters

Implementations of the Protocols in `ports.py`. Chosen only in `app/container.py`. Root: [AGENTS.md](../../../AGENTS.md).

## providers/
- `fake.py`: deterministic offline opponent; `skill` controls difficulty. Default for tests, CI and staging.
- `openai.py`: httpx client; `raise_for_status` maps HTTP errors to domain errors (quota vs retryable, `Retry-After`).
- `retry.py`: `RetryingProvider` wraps any provider with jittered backoff (`backoff_delay`); quota errors are not retried. It fails fast when a retry-after is above the backoff cap or the next try would pass the per-call deadline (`deadline_s`, `Settings.provider_call_deadline`, 120s; the clock is injectable for tests).
- Extend: [add-provider skill](../../../.agents/skills/add-provider/SKILL.md).

## sandbox/
- `common.py:execute`: the single process runner (own process group, wall timeout, streamed output cap, group kill + `cleanup` after every run). Reuse it; don't fork it.
- `runner.py`: in-sandbox loader; flag, source and rlimits go over stdin (`frame`), never argv/env.
- `subprocess_sandbox.py`: rlimits (CPU, AS, FSIZE=0, NOFILE=16, NPROC=1), `python -I -S`, read-only temp dir, minimal env. Fake provider only; not a hard boundary.
- `docker_sandbox.py`: hardened `docker run` (see ADR 0003), limits from `SandboxLimits`, `docker rm -f` after every run. `docker_probe` gates it; real-model matches fail closed without it. Optional `runtime` (e.g. gVisor `runsc`, ADR 0011), checked with `docker_runtimes`. `DEFAULT_IMAGE` (and `Settings.docker_image`, `Dockerfile`, CI) is pinned by digest, and `health()` refuses an image that is not `name@sha256:...` (`unpinned`, R3-7); update all four together. Tests: `tests/integration/test_docker_sandbox.py`, `tests/unit/test_image_pin.py`.
- Never pass the host env through; the child env is only a minimal PATH.

## metrics/
- `jsonl_metrics.py`: `JsonlMetrics` appends one scrubbed JSON line per counter or timing (match id, trace id, tags) to `<store>/metrics.jsonl` under the shared flock; `TeeMetrics` fans out to several sinks. Picked by `Settings.metrics` in `container.build_metrics` (#11).

## store/
- `file_store.py`: atomic JSON save (tmp + fsync + rename, mode 0600) without the secret, which is written once to `<id>.secret` (0600); `load` fails closed if it is missing and migrates pre-M2 files that carried it inline, non-blocking `flock` on `<id>.lock` (`MatchLockedError`); the kernel releases it if the holder dies, so locks never go stale. Never unlink lock files. Match ids must be alphanumeric/hyphen.
- `jsonl.py`: shared flock + fsync append-only JSON-lines helpers used by the ledger and event log.
- `event_log.py`: per-match hash-chained `<id>.events.jsonl`; reads verify the chain (`EventLogCorruptError`); appends truncate a torn tail first (`jsonl.truncate_torn_tail`), unlike the tolerant ledger.
- `ledger.py`: append-only `ledger.jsonl`; every read and append holds `flock`, appends fsync, readers fold only new bytes, a torn last line is skipped and never glued to the next entry. Every call is recorded, $0 included. Totals feed the $40 lifetime cap across processes. Don't delete it to "reset" spend.
