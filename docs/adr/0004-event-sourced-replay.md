# ADR 0004: Event-sourced state and replay
Status: accepted.

- Every change to a match is an `Event` appended to `<store>/<id>.events.jsonl` (`adapters/store/event_log.py`), then applied with `domain/events.apply`, the only code that mutates match state.
- The log is append-only and hash-chained (`sha256(prev + event)`); any edit, deletion or reorder raises `EventLogCorruptError` on read.
- `<id>.json` is a snapshot (with `last_seq`) for fast resume. On resume the engine applies log events past `last_seq`, so a crash between append and save loses nothing.
- Paid attack attempts and build rejections are events, so resume never re-pays them.
- `duel replay` folds the log from scratch, rescores every round and diffs against the snapshot.
- The match secret is never in the log.
