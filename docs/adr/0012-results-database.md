# ADR 0012: Postgres results database (`duel sync`)

Status: accepted.

## Context
Match results live in per-match hash-chained event logs. A leaderboard, history and cost views need a
queryable index, and a future public page needs data that provably holds no flags or secrets.

## Decision
- Neon Postgres is a **derived, rebuildable index**; the event logs stay the source of truth.
  `duel sync --rebuild` truncates and reloads it.
- `duel sync` (owner URL from env `DATABASE_URL` only) verifies each log's chain, loads the snapshot
  without opening `*.secret`, replays it up to `last_seq` and rejects (recorded in `duel.sync_rejects`,
  exit 1) anything inconsistent. Inserts are idempotent, keyed on event hash. If the DB holds a hash
  the log no longer has, sync halts (`HistoryRewrittenError`, exit 2): history was rewritten.
- Only an allowlist of event fields is stored (`domain/public.py`): no challenge source, reference
  payloads, solver output, attack payloads/output, notes or finding excerpts; kept strings are scrubbed.
- Ratings are recomputed with `domain/ratings.py` each run into `duel.rating_snapshots`, with the Elo
  ordering rule (`match_id ascending`), K and the last match id stored alongside.
- Roles live in migrations (`adapters/store/migrations/`), not the Console: `duel_writer` (granted to
  the migrating owner), `duel_reader` (column grants + RLS select policies + `public_v` views) and
  `duel_exporter` (NOLOGIN, member of `duel_reader` only). Every table has FORCE ROW LEVEL SECURITY;
  `public_v` views are `security_invoker`, so the reader's own grants apply.
- v1 does **not** expose Neon publicly (no Data API). `duel export` (env `EXPORT_DATABASE_URL`, the
  exporter login) builds a static JSON on the box: whitelisted columns per view, a manifest of
  `match_id head_hash` plus its sha256, and a secret scan before writing. The exporter refuses any role
  that is superuser, createrole, bypassrls, a writer or a table owner.

## Consequences
- Timestamps are sync time (events carry no wall-clock time).
- Postgres tests run when `DUEL_TEST_DATABASE_URL` points at a throwaway server; CI starts
  `postgres:17-alpine` in Docker for them. `test_sync_export_never_leaks_a_secret` reads the secrets
  in the test only and asserts no secret or derived flag appears in the export or `duel.events`.
- Neon setup (operator): create the project, run `duel sync` with `DATABASE_URL` set to the owner
  URL (applies migrations), then `alter role duel_exporter login password '...'` and set
  `EXPORT_DATABASE_URL` to that login. Keep both URLs out of the repo and CI.
- Transport: `PostgresResults` uses psycopg over 5432. Sync talks only to the `ResultsStore` port, so a
  Neon SQL-over-HTTPS adapter (`POST https://<host>/sql`, `Neon-Connection-String` header) can be added
  behind it if 5432 stays blocked; migrations would then be sent statement by statement.


## Amendments (review fixes, 2026-10-22)

- Migrations are forward-only and checksummed (`duel.schema_migrations`); only `duel db migrate` applies them, under an
  advisory lock, as the owner (`MIGRATE_DATABASE_URL`). `duel sync` refuses a schema that is behind or edited.
- `duel sync` runs as `duel_ingest` (select/insert/update, no delete or truncate); superuser and BYPASSRLS roles are
  refused. `--rebuild` truncates derived tables inside one transaction (rolled back on failure) and needs the owner;
  `duel.match_first_seen` survives rebuilds.
- Sync runs carry status (`running|ok|halted|failed`), `finished_at`, `git_sha` and a ratings digest; ratings are only
  re-snapshotted when inputs change. Rejected matches are flagged and hidden from `public_v`. Daily views are approximate.
- Security: non-local hosts need `sslmode=require` or stricter; reject reasons and DB errors are redacted (class and
  SQLSTATE only); free text is omitted from stored events and legacy logs are checked for plain, rot13 and
  decimal-encoded flags; default EXECUTE is revoked from PUBLIC and any SECURITY DEFINER function in `duel`/`public_v`
  blocks sync and export; CI Postgres is pinned by digest with a random per-job password.

## Amendment: neon-http transport (2026-10-22)

- The results SQL moved to `results_sql.py` behind a small `Executor` protocol; `postgres` (psycopg) and
  `neon-http` (`DUEL_RESULTS_BACKEND=neon-http`) are both transports for it, and `tests/contract` runs one
  suite against memory, postgres and (with `DUEL_TEST_NEON_HTTP_ENDPOINT`, a local proxy) neon-http.
- Neon runs a `{"queries": [...]}` batch as one transaction, so `ingest` and `migrate` stay atomic: `migrate`
  sends the advisory lock, the bookkeeping row and each split statement as one batch.
- No transaction spans requests, so `duel sync --rebuild` and `duel export` stay postgres-only.
