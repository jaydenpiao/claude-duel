-- LLM Duels results index (ADR 0012). A derived copy of the event logs: drop it and `duel sync --rebuild`.
-- Forward-only: an applied file is never edited (its sha256 is checked); rollback is drop schemas, migrate, rebuild.
-- Roles live here, never in a console. duel_writer (granted to the migrating owner) can also delete; the owner
-- alone truncates (`--rebuild`). duel_ingest (NOLOGIN; the operator makes a login that is a member) can only
-- select, insert and update: it is what DATABASE_URL should be for routine `duel sync`.
-- duel_reader reads only the public_v views; they run as the caller (security_invoker), so it also holds
-- column-level SELECT on exactly the columns those views show, and every table forces row-level security.
-- duel_exporter (NOLOGIN here; the operator gives it a password out of band) is only a duel_reader.

do $$ begin
  if not exists (select from pg_roles where rolname = 'duel_writer') then create role duel_writer nologin; end if;
  if not exists (select from pg_roles where rolname = 'duel_ingest') then create role duel_ingest nologin; end if;
  if not exists (select from pg_roles where rolname = 'duel_reader') then create role duel_reader nologin; end if;
  if not exists (select from pg_roles where rolname = 'duel_exporter') then create role duel_exporter nologin; end if;
end $$;
grant duel_reader to duel_exporter;
do $$ begin execute format('grant duel_writer to %I', current_user); end $$;

create schema if not exists duel;
create schema if not exists public_v;
revoke all on schema duel from public;
revoke all on schema public_v from public;

create table duel.matches (
  match_id        text primary key,
  head_hash       char(64) not null,
  event_count     int not null,
  stage           text not null,
  round           int not null,
  mode            text not null,
  challenge_form  text not null,
  framing         text not null,
  rules           jsonb not null,
  limits          jsonb not null,
  failure         text,
  projection_version int not null,
  rejected        boolean not null default false,  -- set when a later sync rejects it; hidden from the views
  synced_at       timestamptz not null default now()
);

-- When each match was first seen. Not derived from the logs, so `--rebuild` keeps it (daily views use it; they
-- are approximate: a sync time, not a play time).
create table duel.match_first_seen (
  match_id        text primary key,
  first_synced_at timestamptz not null default now()
);

create table duel.entrants (
  entrant_id text primary key,
  provider   text not null,
  model      text not null,
  is_fake    boolean not null
);

create table duel.match_agents (
  match_id   text not null references duel.matches on delete cascade,
  agent_name text not null,
  entrant_id text not null references duel.entrants,
  handicap   jsonb not null,
  primary key (match_id, agent_name)
);

create table duel.events (
  event_hash char(64) primary key,
  prev_hash  char(64) not null,
  match_id   text not null references duel.matches on delete cascade,
  seq        int not null,
  kind       text not null,
  v          int not null,
  data       jsonb not null,  -- domain/public.py allowlist only: no source, payloads, output, notes or flags
  unique (match_id, seq)
);

create table duel.calls (
  event_hash    char(64) primary key references duel.events on delete cascade,
  match_id      text not null references duel.matches on delete cascade,
  agent         text not null,
  model         text not null,
  tokens        int not null,
  input_tokens  int not null,
  output_tokens int not null,
  usd           numeric(12, 6) not null,
  elapsed_ms    int not null
);

create table duel.attacks (
  event_hash   char(64) primary key references duel.events on delete cascade,
  match_id     text not null references duel.matches on delete cascade,
  challenge_id text not null,
  round        int not null,
  author       text not null,
  attacker     text not null,
  solved       boolean not null,
  attempts     int not null,
  tokens_used  int not null,
  usd_micros   bigint not null
);

create table duel.round_scores (
  event_hash char(64) not null references duel.events on delete cascade,
  match_id   text not null references duel.matches on delete cascade,
  round      int not null,
  agent      text not null,
  points     int not null,
  primary key (event_hash, agent)
);

create table duel.match_results (
  match_id text primary key references duel.matches on delete cascade,
  winner   text,
  scores   jsonb not null,
  calls    int not null,
  tokens   bigint not null,
  usd      numeric(12, 6) not null
);

create table duel.sync_runs (
  id             text primary key check (id ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'),  -- client-generated uuid
  started_at     timestamptz not null default now(),
  finished_at    timestamptz,
  status         text not null default 'running' check (status in ('running', 'ok', 'halted', 'failed')),
  git_sha        text,
  ratings_digest text,  -- inputs behind this run's ratings; an unchanged digest writes no new snapshot
  inserted    int not null default 0,
  skipped     int not null default 0,
  rejected    int not null default 0
);

create table duel.sync_rejects (
  sync_run_id text not null references duel.sync_runs on delete cascade,
  match_id    text not null,
  reason      text not null,
  primary key (sync_run_id, match_id)
);

create table duel.rating_snapshots (
  sync_run_id   text not null references duel.sync_runs on delete cascade,
  entrant_id    text not null,  -- no FK: snapshots outlive a `--rebuild` of entrants
  games         int not null,
  elo           double precision not null,
  bt            double precision not null,
  bt_lo         double precision not null,
  bt_hi         double precision not null,
  elo_order     text not null,  -- Elo is order-dependent: the fold order, so history reproduces exactly
  elo_k         double precision not null,
  through_match text not null,
  primary key (sync_run_id, entrant_id)
);

create index on duel.calls (model);
create index on duel.attacks (match_id);

create view public_v.matches with (security_invoker = true) as
  select m.match_id, m.head_hash, m.event_count, m.stage, m.round, m.mode, m.challenge_form, m.framing,
         f.first_synced_at, r.winner, r.scores, r.calls, r.tokens, r.usd
  from duel.matches m join duel.match_results r using (match_id) join duel.match_first_seen f using (match_id)
  where not m.rejected;

create view public_v.match_agents with (security_invoker = true) as
  select a.match_id, a.agent_name, a.entrant_id
  from duel.match_agents a join duel.matches m using (match_id) where not m.rejected;

create view public_v.rating_history with (security_invoker = true) as
  select s.sync_run_id, r.finished_at as computed_at, s.entrant_id, s.games, s.elo, s.bt, s.bt_lo, s.bt_hi,
         s.elo_order, s.elo_k, s.through_match
  from duel.rating_snapshots s join duel.sync_runs r on r.id = s.sync_run_id
  where r.status = 'ok';

create view public_v.leaderboard with (security_invoker = true) as
  select * from public_v.rating_history
  where sync_run_id = (select sync_run_id from public_v.rating_history order by computed_at desc limit 1);

create view public_v.model_daily with (security_invoker = true) as
  select date_trunc('day', f.first_synced_at) as day, a.entrant_id,
         count(*) filter (where k.attacker = a.agent_name) as attacks,
         count(*) filter (where k.attacker = a.agent_name and k.solved) as captures,
         count(*) filter (where k.author = a.agent_name) as defended_challenges,
         count(*) filter (where k.author = a.agent_name and not k.solved) as defenses
  from duel.attacks k join duel.matches m using (match_id) join duel.match_first_seen f using (match_id)
       join duel.match_agents a using (match_id)
  where not m.rejected
  group by 1, 2;

create view public_v.cost_daily with (security_invoker = true) as
  select date_trunc('day', f.first_synced_at) as day, c.model, sum(c.usd) as usd, sum(c.tokens) as tokens,
         count(*) as calls, count(distinct c.match_id) as matches
  from duel.calls c join duel.matches m using (match_id) join duel.match_first_seen f using (match_id)
  where not m.rejected
  group by 1, 2;

do $$
declare t text;
begin
  foreach t in array array['matches', 'match_first_seen', 'entrants', 'match_agents', 'events', 'calls', 'attacks',
                           'round_scores', 'match_results', 'sync_runs', 'sync_rejects', 'rating_snapshots'] loop
    execute format('alter table duel.%I enable row level security', t);
    execute format('alter table duel.%I force row level security', t);
    execute format('create policy writer_all on duel.%I for all to duel_writer, duel_ingest using (true) with check (true)', t);
    execute format('grant select, insert, update, delete on duel.%I to duel_writer', t);
    execute format('grant select, insert, update on duel.%I to duel_ingest', t);
  end loop;
  foreach t in array array['matches', 'match_first_seen', 'match_results', 'match_agents', 'attacks', 'calls',
                           'rating_snapshots', 'sync_runs'] loop
    execute format('create policy reader_select on duel.%I for select to duel_reader using (true)', t);
  end loop;
end $$;
grant usage on schema duel to duel_writer, duel_ingest, duel_reader;
grant select on duel.schema_migrations to duel_writer, duel_ingest;
alter table duel.schema_migrations enable row level security;
alter table duel.schema_migrations force row level security;
create policy migrator_all on duel.schema_migrations for all to current_user using (true) with check (true);
create policy sync_read on duel.schema_migrations for select to duel_writer, duel_ingest using (true);
grant select (match_id, head_hash, event_count, stage, round, mode, challenge_form, framing, rejected)
  on duel.matches to duel_reader;
grant select (match_id, first_synced_at) on duel.match_first_seen to duel_reader;
grant select (match_id, winner, scores, calls, tokens, usd) on duel.match_results to duel_reader;
grant select (match_id, agent_name, entrant_id) on duel.match_agents to duel_reader;
grant select (match_id, attacker, author, solved) on duel.attacks to duel_reader;
grant select (match_id, model, usd, tokens) on duel.calls to duel_reader;
grant select (sync_run_id, entrant_id, games, elo, bt, bt_lo, bt_hi, elo_order, elo_k, through_match)
  on duel.rating_snapshots to duel_reader;
grant select (id, finished_at, status) on duel.sync_runs to duel_reader;
grant usage on schema public_v to duel_reader;
grant select on all tables in schema public_v to duel_reader;

-- No function is callable by default: Postgres grants EXECUTE on new functions to PUBLIC, and a SECURITY DEFINER
-- one would bypass RLS and column grants. Defaults are locked for the migrating owner (a
-- per-schema revoke is a no-op in Postgres, so the global default is used), and `duel sync`/`duel export` refuse
-- to run while any SECURITY DEFINER function exists in duel or public_v (postgres.py `refuse_definer_functions`).
revoke all on all functions in schema duel, public_v from public;
alter default privileges revoke execute on functions from public;
