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
spend more tokens, and the result was scored as a failed attempt rather than a
declined turn.

## Decision
A vendor refusal is a forfeited turn, not a weak attack.

- Providers set `Completion.refused`. The call is still charged (the vendor billed it).
- `call_charged.refused` is written only when true, the same omit-if-absent rule PR 282
  used for `round_scored.heat_bound`, so older logs stay byte-identical and keep their
  hash chain. Readers treat a missing field as false. Schema version stays 2.
- There is no second paid call (unlike a truncated reply). The refusal body is not
  parsed as an answer and is not stored.
- Build: the challenge is forfeited immediately (`forfeit_reason` is the stable
  `REFUSED` string; the author pays `invalid_penalty`). Format retries are skipped.
- Attack: one attempt is recorded (`output` is `REFUSED`) so resume does not re-pay,
  then the attack ends. A valid challenge that was not captured still pays its defense
  award. Further tries are not spent.
- Allocate (ledger): even-split fallback, same as an invalid or truncated allocation.
- Difficulty gate: a refusal is not a capture (the probe did not solve it).
- Replay, the match card, the arena, and the public event projection label a refusal
  when the field is present; matches with none look as they did before.

## Consequences
A model that declines a build is penalized like any other forfeit, and a model that
declines an attack does not capture. That is the existing forfeit math, not a new
scoring table. Old logs replay and rescore unchanged. Real-model spend is unchanged
until a match is actually run; this ADR adds no vendor calls.
