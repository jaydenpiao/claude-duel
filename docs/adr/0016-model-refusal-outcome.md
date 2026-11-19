# ADR 0016: model refusal outcome

Status: accepted.

## Context
Anthropic's Messages API can decline to answer with HTTP 200 and `stop_reason` `"refusal"`
(safety classifiers; see [Handling stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)).
OpenAI's equivalents are a chat `finish_reason` of `"content_filter"`, a non-empty
`message.refusal` (or a `refusal` content part), and on the Responses API a refusal
output part or `incomplete_details.reason == "content_filter"`.

The adapters only treated a token-cap stop as special (`truncated`). A refusal then
looked like a weak or malformed answer: format retries or extra attack tries could
spend more tokens, and an author's challenge could print the engine's refusal
sentinel and end an opponent's attack after one try.

## Decision
A vendor refusal is a typed outcome (`Completion.refused`, `call_charged.refused`,
`AttackAttempt.refused`). Control flow never compares run output to an engine
sentinel; challenge stdout that would equal one is rewritten before it is stored.

- Providers set `Completion.refused`. Every call is still charged (the vendor billed it).
- `call_charged.refused` and `AttackAttempt.refused` are written only when true, the
  same omit-if-absent rule PR 282 used for `round_scored.heat_bound`, so older logs
  stay byte-identical and keep their hash chain. Readers treat a missing field as
  false. Schema version stays 2.
- There is no second paid call in the same try (unlike a truncated reply). The
  refusal body is not parsed as an answer and is not stored.
- Build: the challenge is forfeited immediately (`forfeit_reason` is the stable
  `REFUSED` string; the author pays `invalid_penalty`). Format retries are skipped.
- Attack: a refusal is one spent try (`AttackAttempt.refused`). The attack continues
  with the remaining tries. A valid challenge that was not captured still pays its
  defense award.
- Allocate (ledger): even-split fallback, same as an invalid or truncated allocation.
- Difficulty gate: a refused probe is inconclusive, not held and not solved. The
  challenge stays valid and is not marked `probe_solved`.
- Replay, the match card, the arena, and the public event projection label a refusal
  when the field is present; matches with none look as they did before.

## Consequences
A model that declines a build is penalized like any other forfeit. A model that
declines an attack spends that try and may still capture on a later one; if it
never captures, the defense award still pays. Old logs replay and rescore
unchanged. Real-model spend is unchanged until a match is actually run; this ADR
adds no vendor calls.
