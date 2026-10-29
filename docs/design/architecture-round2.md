# claude-duel: architecture re-review, round 2 (2026-10-21)

**Commit reviewed:** `66dbefc` ("add readable match output (#8)"). This is HEAD of `main`, and the working tree is clean. I left the repo untouched and ran the tests in a throwaway clone.

**Baseline:** round 1 architecture review of 2026-10-21 (23 findings), checked against fix PRs #2–#8.

## Test and tool results (throwaway clone, Python venv, `pip install -e .[dev]`)
- `pytest`: **151 passed, 1 deselected** (the `live` marker). Coverage was 98.93%, above the 90% gate.
- `ruff check src tests`: clean. `mypy src` (strict): no issues in 37 files.
- I also ran three probes outside the test suite, all confirmed:
  - The policy accepts `re.enum.sys.modules["os"].system(...)` and `json.codecs.open(...)` (check returns `None`). See R1.
  - A torn tail followed by one more append makes every later `read()` raise `EventLogCorruptError`. See N1.
  - A crash after the `match_created` append, before the snapshot is saved, makes `play()` fail with `event seq 0 != expected 1` on every resume. See N2.

## Round-1 findings: status

| # | Sev (r1) | Status | Evidence |
|---|---|---|---|
| 1 | blocker | **Partly fixed** | `policy.py:14-25,84-98` is now an allowlist: `_`-prefixed names and attributes are banned, and only `sys.stdin`/`stdout` are allowed. But attribute access on the *pure* modules is unrestricted (`policy.py:95-96` only checks `RESTRICTED_MODULES`), so `re.enum.sys.modules["os"]` and `json.codecs.open` pass (confirmed). Docker (finding 2) still contains the damage, so the residual is **major**. Fix: restrict attributes for every module to a per-module allowlist, or ban attribute chains deeper than `module.func`. |
| 2 | blocker | **Fixed** | `container.py:38-52`: fails closed for non-fake agents. `docker_sandbox.py:23-56` checks `docker info`, seccomp, and runs a self-test container. |
| 3 | major | **Fixed** | `docker_sandbox.py:62,82,89,99-105`: each run gets a unique `--name` plus `docker rm -f` cleanup; `timeout` runs inside the container (`:78`). |
| 4 | major | **Fixed** | `runner.py` `frame()` and `docker_sandbox.py:58-59,85`: flag and source go over stdin, not argv or env. |
| 5 | major | **Fixed** | `file_store.py:13-40`: `flock` on a held fd; the kernel releases it if the process dies. |
| 6 | major | **Fixed** | `session.py:24-41`: the lock wraps load, create and run. `engine.py:71` documents that the caller holds the lock. |
| 7 | major | **Fixed** | `container.py:67-91` with `session.py:31,41`: agents and models are pinned in state, and the engine is built from `state.agents`; drift only logs a warning (`session.py:44-52`). |
| 8 | major | **Fixed** | `ledger.py:24-40`: the ledger is re-read under a shared or exclusive `flock` and every call is recorded (budget `charge`, no `usd>0` guard). Run caps are documented as per-process (budget.py docstring). |
| 9 | major | **Fixed** | `event_log.py`, `domain/events.py:128-162`, `replay.py:23-44`: hash-chained JSONL log; `apply` is the only mutator; replay folds from scratch. New issues are listed as N1–N5 and N9. |
| 10 | major | **Open** | `container.py:27-35` still supports only `fake` and `openai`; `requirements.md:12` and `design.md:6` still promise Anthropic. |
| 11 | major | **Partly fixed** | Added provider and sandbox timings plus kill counters (`engine.py:130-145`) and an end-of-run summary log (`cli.py:68`, `observability.py:105-108`). There is still no exportable metrics sink (EMF/StatsD), and tagged series are dropped from `summary()`. |
| 12 | major | **Fixed** | Per-attempt and per-rejection events: `engine.py:163-176,214-237`. A small residual window remains (N10). |
| 13 | minor | **Fixed** | `state_machine.py:17-31`: explicit `_NEXT`. |
| 14 | minor | **Open** | No invariant check on load or recover (`state_machine.py:46-50`). `transition(FAILED)` from a terminal stage still raises (`:35-40`), and `engine.py:81-83` can still mask the original exception (see N4). |
| 15 | minor | **Fixed** | `engine.py:251-252` checks `scored_rounds`. |
| 16 | minor | **Fixed** | `engine.py:78,85`: the stage is captured before the transition. |
| 17 | minor | **Fixed** (doc drift) | `engine.py:158-169`: provider errors fail the stage, which is resumable. But `requirements.md:26` still says a permanent 4xx means "unit forfeited, match continues", and a deterministic 4xx now makes the match fail again on every resume (N12). |
| 18 | minor | **Open** | `retry.py:42-65` is unchanged: no deadline and no fail-fast on long `retry-after`. |
| 19 | minor | **Open** | `config.py:13` still imports `app.budget` (and now `FAKE_MODEL` too). |
| 20 | minor | **Fixed** | `container.py:55-64`: `build_store`, `build_event_log` and `build_ledger` are split out; `cli.py:99-105`. |
| 21 | minor | **Open** | `config.py:43-62`: still no env override for `provider_max_retries`, `lifetime_max_usd`, `run_max_calls` or `key_limit_usd`; `log_level` and `provider_timeout` are still unvalidated (`:26,30`). |
| 22 | minor | **Fixed** (for the ledger) | `jsonl.py:23-32` adds a leading newline, and `ledger.py:43-47` skips bad lines. The same primitive breaks the event log (N1). |
| 23 | minor | **Fixed** | `common.py:33-41,55,66-70`: new session and `killpg` on every run; `RLIMIT_NPROC=1` (`subprocess_sandbox.py:24`). |

**Tally: Fixed 16** (2–9, 12, 13, 15, 16, 17, 20, 22, 23) · **Partly fixed 2** (1, 11) · **Open 5** (10, 14, 18, 19, 21). Total 23.

## New findings

### Event log

**N1. MAJOR: a crash mid-write permanently corrupts the event log.** `jsonl.py:23-32`, `event_log.py:36-44,55-65`. `read_complete` hides a torn tail, but `append` then writes `"\n" + record`. That turns the torn fragment into a complete, unparseable line, so `_verify` raises `EventLogCorruptError("unreadable event line n")` on every later read, resume or replay (confirmed). The `\n`-prefix trick is correct for the tolerant ledger and wrong for a verified chain. **Fix:** under the exclusive lock, `ftruncate(fd, complete_offset)` to drop the torn tail (it was never acknowledged, so the snapshot can't be ahead of it) and fsync before appending. Add a test that truncates mid-line.

**N2. MAJOR: a crash between the first append and the first snapshot save makes the match unresumable.** `session.py:28-35` saves a snapshot with `last_seq=-1`. `engine.py:98-99` then appends `match_created` (seq 0) and saves. If the process dies in between, the next `_sync` sees `last_seq<0` and appends seq 0 again, which `event_log.py:40-42` rejects (confirmed: `event seq 0 != expected 1`). **Fix:** in `_sync`, always read the log first. If the log is non-empty, fold or apply it regardless of `last_seq`. Only emit `match_created` when the log is empty.

**N3. MAJOR: events are appended before they're validated, so one bad event poisons the log.** `engine.py:116-120`. `_record` makes the event durable, then calls `ev.apply`. If the handler raises (an invalid `transition` in `events.py:58-63`, a model `ValidationError` in `:81-96`, or a `KeyError`), the event stays in the log, and every resume or replay re-applies it and fails forever. **Fix:** apply to a `state.model_copy(deep=True)` first, append only if that succeeds, then swap the state in. Or validate typed payloads (N5) before appending.

**N4. MAJOR: observer failures break the engine, despite "observers never affect the match".** `event_log.py:71-80`, `engine.py:116-120,81-83`. The observer runs inside `append`, before `apply`. If commentary raises (for example `BrokenPipeError` from `duel run | head`, or the `assert` or `KeyError` paths in `commentary.py:115-116,125-126`), state stays one event behind the log. The `except` path then tries `MATCH_FAILED` with a duplicate seq, gets an `EventLogCorruptError`, and the original error is masked and no snapshot is saved. **Fix:** notify observers from the engine *after* `apply` and save, wrapped in `try/except Exception` that logs and counts `observer.error`. Never let an observer raise into the write path.

**N5. MAJOR: no schema versioning, and payloads are untyped.** `models.py:123-127` (`data: dict[str, object]`), `events.py:46-101` (`cast`/`str()`/`int()` coercion), and unknown types hard-fail (`events.py:131-133`). Neither the log record (`event_log.py:43`) nor the snapshot carries a version, so renaming a field or adding an event type breaks every existing log with no migration path. **Fix:** add `"v": 1` to each record and to `MatchState`, define one pydantic model per event type (a discriminated union on `type`), validate on write and read, and add an upcaster registry keyed by `(type, v)`.

**N6. MINOR: each append is O(n), so a match is O(n²).** `event_log.py:37-39`. Every append re-reads and re-hashes the whole file under `LOCK_EX`. A 20-round × 10-challenge × 50-attempt match makes this noticeable. **Fix:** cache `(size, last_seq, head_hash)` per match and re-verify only when the file size differs from the cache.

**N7. MINOR: directories are never fsynced.** `jsonl.py:15` (creating the log and ledger), `file_store.py:61` (`os.replace`). After a power loss the new file or rename can disappear even though the file contents were fsynced. **Fix:** `os.fsync(os.open(dir, O_RDONLY))` after create and replace.

**N8. MINOR: the hash chain is only tamper-evident against naive edits.** `event_log.py:21-23,55-68`. The chain is unkeyed and the snapshot doesn't pin the head hash, so anyone who can write the file can rewrite it and recompute every hash. ADR 0004 (line 4: "any edit, deletion or reorder raises") overclaims. **Fix:** store `head_hash` in the snapshot and check it in `_sync`/replay, or HMAC the chain with a key that isn't in the store. At minimum, reword the ADR.

**N9. MINOR: replay checks only part of the state.** `replay.py:11,28`. The comparison skips `build_errors`, `scored_rounds`, `failure`, `failed_stage` and `last_seq`, and silently ignores log events past `snapshot.last_seq` instead of reporting them. **Fix:** compare the full model minus `secret`, and report "log ahead by k events" as an explicit note.

**N10. MINOR: a crash between `CALL_CHARGED` and the unit's result event re-pays the call.** `engine.py:135` vs `:156,174,237`. The completion text isn't persisted, so a crash during the sandbox run (up to `wall_seconds + 5s` per attempt) repeats the paid call on resume. **Fix:** put `completion.text` (redacted where needed) into `call_charged`, and have the resumed unit consume a pending, unconsumed completion before calling the provider again.

### Readable-output layer (`presentation/commentary.py`)

**N11. MINOR: commentary duplicates the scoring rules and event parsing.** `commentary.py:108` reimplements `invalid_penalty * round` from `scoring.py:23`, and `commentary.py:95,105,124` re-parse `e.data` exactly as `events.py:81-96` does. Separating it from the domain is otherwise good: it depends only on `domain.events`/`flags`/`scoring`, has no I/O beyond the injected `TextIO`, and engine, replay and commentary all share one `apply`. **Fix:** add `invalid_penalty(round, rules)` to `scoring.py`, and once N5 lands, have handlers receive typed event payloads.

**N12. MINOR: docs drift after the round-1 fixes.** `requirements.md:26` (permanent 4xx means forfeit, but the code now fails the match, and a deterministic 4xx loops on resume); `design.md:6` (Anthropic, "atomic JSON files"); `design.md:13` (should mention the log); ADR 0004 line 4 (see N8). **Fix:** update the docs. Decide whether a permanent 4xx after N attempts should forfeit the unit so the match can't be stuck.

**N13. MINOR: the CLI's text and JSON modes behave differently.** `cli.py:69-74`. Text mode re-reads the store and the log after the lock is released and runs replay (exit code 1 on mismatch). JSON mode skips replay. Commentary's `_SECRETISH` (`commentary.py:20`) also redacts any 32+ hex string, which mangles the sha256 in hash challenges. **Fix:** run the same verification in both modes, inside the session. Limit the hex redaction to `FLAG_PATTERN` plus known key shapes.

## Counts
- Round-1 status: **Fixed 16 · Partly fixed 2 (#1, #11) · Open 5 (#10, #14, #18, #19, #21)**
- New findings: **Blocker 0 · Major 5 (N1–N5) · Minor 8 (N6–N13)**

## Top 3 priorities
1. Make the event log crash-safe: truncate torn tails (N1), handle a log that is ahead at creation (N2), and validate before append (N3). Today a single badly timed crash or bad event can brick a match, which undermines the round-1 resumability fixes.
2. Isolate observers from the write path (N4) and add schema versioning with typed event payloads (N5) before the first real-model logs exist.
3. Close the remaining policy bypass by restricting attribute access on pure modules (#1 residual, major), then the Anthropic and docs gap (#10, N12).
