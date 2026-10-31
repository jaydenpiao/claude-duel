# adapters

Implementations of the Protocols in `ports.py`. Chosen only in `app/container.py`. Root: [AGENTS.md](../../../AGENTS.md).

## providers/
- `fake.py`: deterministic offline opponent; `skill` controls difficulty. Default for tests, CI and staging.
- `openai.py`: httpx client; gpt-6/gpt-5.6 go through the Responses API (no temperature, strict `json_schema`, `reasoning.effort`), older models through Chat Completions JSON mode; a reply stopped at the cap is `truncated`.
- `anthropic.py`: Messages API client on the same port; no temperature or cache_control, cache tokens billed as input; 529 is transient, "credit balance" is quota. Sends the strict `json_schema` and `effort` via `output_config`; `stop_reason == "max_tokens"` is `truncated`; thinking tokens are reported in `reasoning_tokens` (already inside output, billed as output).
- `http.py`: `raise_for_status(resp, vendor)`, shared by every HTTP adapter; maps HTTP errors to domain errors (quota vs retryable, `Retry-After`).
- `registry.py`: `FACTORIES`, vendor name -> adapter factory; read only by `app/container.py`. Vendor metadata (prefix, key env, default model) is `domain/vendors.VENDORS` (ADR 0013, quality M11).
- `retry.py`: `RetryingProvider` wraps any provider with jittered backoff (`backoff_delay`); quota errors are not retried. It fails fast when a retry-after is above the backoff cap or the next try would pass the per-call deadline (`deadline_s`, `Settings.provider_call_deadline`, 120s; the clock is injectable for tests).
- Extend: [add-provider skill](../../../.agents/skills/add-provider/SKILL.md).

## sandbox/
- `common.py:execute`: the single process runner (own process group, wall timeout, streamed output cap, group kill + `cleanup` after every run). Reuse it; don't fork it.
- `runner.py`: in-sandbox loader; flag, source and rlimits go over stdin (`frame`), never argv/env.
- `subprocess_sandbox.py`: rlimits (CPU, AS, FSIZE=0, NOFILE=16, NPROC=1), `python -I -S`, read-only temp dir, minimal env. Fake provider only; not a hard boundary.
- `docker_sandbox.py`: hardened `docker run` (see ADR 0003), limits from `SandboxLimits`, `docker rm -f` after every run. `docker_probe` gates it (also refusing a daemon with cgroup driver `none`, which would ignore the limits); real-model matches fail closed without it. Optional `runtime` (e.g. gVisor `runsc`, ADR 0011), checked with `docker_runtimes`. `DEFAULT_IMAGE` (and `Settings.docker_image`, `Dockerfile`, CI) is pinned by digest, and `health()` refuses an image that is not `name@sha256:...` (`unpinned`); update all four together. Tests: `tests/integration/test_docker_sandbox.py`, `tests/unit/test_image_pin.py`, `tests/unit/test_docker_probe.py`.
- Never pass the host env through; the child env is only a minimal PATH.

## metrics/
- `jsonl_metrics.py`: `JsonlMetrics` appends one scrubbed JSON line per counter or timing (match id, trace id, tags) to `<store>/metrics.jsonl` under the shared flock; `TeeMetrics` fans out to several sinks. Picked by `Settings.metrics` in `container.build_metrics` (#11).

## store/
- `file_store.py`: atomic JSON save (tmp + fsync + rename, mode 0600) without the secret, which is written once to `<id>.secret` (0600); `load` fails closed if it is missing and migrates older files that carried it inline, non-blocking `flock` on `<id>.lock` (`MatchLockedError`); the kernel releases it if the holder dies, so locks never go stale. Never unlink lock files. Match ids must be alphanumeric/hyphen.
- `jsonl.py`: shared flock + fsync append-only JSON-lines helpers used by the ledger and event log.
- `results_sql.py` (ADR 0012): the results SQL shared by every transport: `Executor` protocol (`query`, atomic `batch`, `transaction`), migrations (`split_statements`, checksummed `migrate` as one lock-first batch), the role guards and `SqlResults` (`ResultsStore`). `tests/contract` runs one suite over every transport.
- `postgres_url.py`: `require_tls` and `iam_url` (escapes the `@` in a Cloud SQL IAM user) with no driver import.
- `postgres.py` (ADR 0012): `PgExecutor`, `connect` (requires `sslmode=require`+ for non-local hosts via `require_tls`; errors never echo the URL; `DATABASE_URL`, `MIGRATE_DATABASE_URL` and `EXPORT_DATABASE_URL` are env-only secrets, see root AGENTS.md), `migrate` (forward-only, checksummed in `duel.schema_migrations`, one transaction under an advisory lock; only `duel db migrate` calls it) and `require_current`, `refuse_superuser`, `refuse_definer_functions`, `db_errors` (psycopg errors become `ResultsDbError` with class and SQLSTATE only), `PostgresResults` (`ResultsStore`; idempotent inserts keyed on event hash, `rebuilding()` truncates derived tables in one transaction) and `PublicReader` (`PublicSource`; refuses superuser, createrole, bypassrls, `duel_writer`/`duel_ingest` members and table owners; reads `public_v` in a read-only transaction). Roles, FORCE RLS and `security_invoker` views live in SQL, never the Console. New migrations are new files; never edit an applied one.
- `event_log.py`: `chain(id)` returns verified `(hash, event)` pairs for `duel sync`; per-match hash-chained `<id>.events.jsonl`; reads verify the chain (`EventLogCorruptError`); appends truncate a torn tail first (`jsonl.truncate_torn_tail`), unlike the tolerant ledger.
- `ledger.py`: append-only `ledger.jsonl`; every read and append holds `flock`, appends fsync, readers fold only new bytes, a torn last line is skipped and never glued to the next entry. Every call is recorded, $0 included. Totals feed the $33 lifetime cap across processes. Don't delete it to "reset" spend.
