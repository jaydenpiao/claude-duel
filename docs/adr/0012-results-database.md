# ADR 0012: Postgres results database (`duel sync`)

Status: accepted.

## Context
Match results live in per-match hash-chained event logs. A leaderboard, history and cost views need a
queryable index, and a future public page needs data that provably holds no flags or secrets.

## Decision
- Cloud SQL for PostgreSQL is a **derived, rebuildable index**; the event logs stay the source of truth.
  `duel sync --rebuild` truncates and reloads it.
- `duel sync` (a `duel_ingest` login from env `DATABASE_URL`; the owner's `MIGRATE_DATABASE_URL` only for
  `--rebuild`) verifies each log's chain, loads the snapshot
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
- v1 does **not** expose the database publicly (no public IP, no authorized networks). `duel export` (env `EXPORT_DATABASE_URL`, the
  exporter login) builds a static JSON on the box: whitelisted columns per view, a manifest of
  `match_id head_hash` plus its sha256, and a secret scan before writing. The exporter refuses any role
  that is superuser, createrole, bypassrls, a writer or a table owner.

## Consequences
- Timestamps are sync time (events carry no wall-clock time).
- Postgres tests run when `DUEL_TEST_DATABASE_URL` points at a throwaway server; CI starts
  `postgres:17-alpine` in Docker for them. `test_sync_export_never_leaks_a_secret` reads the secrets
  in the test only and asserts no secret or derived flag appears in the export or `duel.events`.
- Cloud SQL setup (operator): one IAM principal per role, never one login for all. An ingest service account is
  an IAM DB user that is only a member of `duel_ingest` (`DATABASE_URL`); an exporter service account is only in
  `duel_reader` (`EXPORT_DATABASE_URL`); the schema owner is used only for `duel db migrate` and `--rebuild`,
  through a short-lived proxy (or `gcloud sql connect`) that is stopped afterwards. Each
  `cloud-sql-proxy --auto-iam-authn` listens on a Unix socket (`--unix-socket`) in a 0700 directory owned by the
  operator, never on TCP loopback and never as the CI runner's uid, so other local processes can't borrow its
  login. Routine `duel sync` refuses a role that owns a `duel` table (`refuse_table_owner`), and the export refuses
  any write or owner role. Keep every URL and key out of the repo and CI.
- Transport: `PostgresResults` uses psycopg to the local proxy, which dials Google with IAM + TLS.


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

## Amendment: Cloud SQL transport (2026-10-29)

- The results SQL lives in `results_sql.py` behind a small `Executor` protocol, run by the `postgres` (psycopg)
  transport; `tests/contract` runs one suite against memory and postgres. The Neon SQL-over-HTTPS transport
  was dropped: the index moved to Cloud SQL for PostgreSQL.
- The box connects through `cloud-sql-proxy --auto-iam-authn --unix-socket <dir>` (a 0700 directory; TLS and IAM
  handled by the proxy; socket hosts are exempt from `require_tls`). `DATABASE_URL` uses the ingest IAM user and no
  password, e.g. `postgresql://duel-ingest@<project>.iam@/duel?host=<dir>/<connection-name>`; `iam_url` escapes the
  inner `@` for libpq.
