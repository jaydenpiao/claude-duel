# ADR 0009: between-round memory

Status: accepted (closes #19; spec: docs/design/design-memory-strategy.md)

## Decision
- `Rules.memory` (`MemoryRule`): `kind` `none` (default) or `summary`; `max_chars` (3000, about 750 tokens)
  and `source_chars` (600 per opponent challenge).
- `domain/memory.summarize` is a pure function of folded state, so replay reproduces it exactly.
  It lists your own outcomes, the opponent's past challenges (title, difficulty, static technique tag,
  truncated source), opponent tendencies and the scoreboard.
- The opponent's source is shown because the attacker already saw it during the attack. The summary never
  includes `solver` or `reference_payload` or flags. The engine runs `assert_no_flag` against every
  challenge flag before the summary goes into a prompt.
- When the summary is over the cap, the oldest rounds are dropped first and marked `[older rounds omitted]`.
- The summary goes into build and attack prompts as a fenced, data-only MEMORY block. With `none` the
  prompts are byte-identical to before (the commentary snapshot is unchanged).
