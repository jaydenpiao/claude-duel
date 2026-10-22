# ADR 0009: between-round memory

Status: accepted (closes #19; spec: docs/design/design-memory-strategy.md)

## Decision
- `Rules.memory` (`MemoryRule`): `kind` `none` (default) or `summary`; `max_tokens` (1200) for the block
  and `source_tokens` (800, shared by all revealed sources), counted by `memory.est_tokens` (ceil of UTF-8
  bytes / 4, no vendor tokenizer). Logged rules with the older `max_chars`/`source_chars` load as bytes / 4.
- `domain/memory.summarize` is a pure function of folded state, so replay reproduces it exactly.
  It lists your own outcomes, the opponent's past challenges (title, difficulty, static technique tag,
  truncated source), opponent tendencies and the scoreboard.
- The opponent's source is shown because the attacker already saw it during the attack. The summary never
  includes `solver` or `reference_payload` or flags. The engine runs `assert_no_flag` against every
  challenge flag before the summary goes into a prompt.
- An opponent source is shown only once its round has `round_scored`. Each gets `source_tokens // n` (at
  least 64, never past the shared total), cut at a line boundary with `# [truncated: K of N lines shown]`, or
  replaced by `# [source omitted: budget]`.
- Over `max_tokens`, drop in order: oldest rounds (marked `[older rounds omitted]`), tendencies, your last
  payloads, then sources oldest first. The scoreboard and the latest round's outcomes stay.
- The summary goes into build and attack prompts as a fenced, data-only MEMORY block. With `none` the
  prompts are byte-identical to before (the commentary snapshot is unchanged).
