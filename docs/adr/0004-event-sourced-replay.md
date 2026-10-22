# ADR 0004: Event-sourced state and replay
Status: accepted.

- Every change to a match is an `Event` appended to `<store>/<id>.events.jsonl` (`adapters/store/event_log.py`), then applied with `domain/events.apply`, the only code that mutates match state.
- The log is append-only and hash-chained (`sha256(prev + event)`); any edit, deletion or reorder raises `EventLogCorruptError` on read.
- `<id>.json` is a snapshot (with `last_seq`) for fast resume. On resume the engine applies log events past `last_seq`, so a crash between append and save loses nothing.
- Durability: an event is applied to a copy first and appended only if that succeeds; appends fsync; a torn trailing line (crash mid-write, never acknowledged) is truncated before the next append; on resume the log, not `last_seq`, decides whether `match_created` exists.
- Paid attack attempts and build rejections are events, so resume never re-pays them.
- `duel replay` folds the log from scratch, rescores every round and diffs against the snapshot.
- The match secret is never in the log.

**Schema version and typed payloads (N5).** Every event carries `v` (`events.SCHEMA_VERSION`, currently 1), and each event type has a frozen pydantic payload model with `extra="forbid"` (`events.MatchCreated`, `StageChanged`, and so on). `events.payload` validates the payload before a handler runs. A wrong shape, an unknown type, or a version newer than this build counts as `EventLogCorruptError`, and the engine finds this out before persisting (N3). Events written before versioning have `v = 0`, the same payload shapes, and no `v` key on disk, so their hash chain still verifies. A future payload change raises `SCHEMA_VERSION` and adds an upcaster from the older version in `events.payload`.
