# Design: between-round memory (issue #19 "agents adapt between rounds")
Depends on: design-rules-abstraction.md (`Rules.memory`). Aligns with ADR 0006 (solver and reference payload are never shown).

## Goal
In round N+1, each agent gets a bounded, flag-free, fenced summary of rounds 1..N so its strategy can evolve: what it captured, what held, what it built that got cracked, and how the opponent attacks. It also gets an optional private scratchpad it can write to.

## What goes in (derived, deterministic)
`domain/memory.py::summarize(state, agent, upto_round, rule) -> str`, a pure function of state (which is a function of the log), so replay reproduces it exactly. Sections, in fixed order and sorted by challenge id:
1. **Your challenges**: title, difficulty, outcome (`held` / `captured on attempt k` / `invalid: <reason>`), and the opponent's winning payload if captured. Never the source again (they wrote it), never `solver` or `reference_payload`.
2. **Opponent's challenges you attacked**: title, difficulty, outcome, your attempts count, last payload. A compact "technique tag" comes from a cheap static classifier over the opponent's source AST (`maze`, `hash-check`, `checksum`, `state-machine`, `vm`, `other`), done with `ast`, no LLM, deterministic.
3. **Opponent tendencies**: counts of the opponent's tags so far, and their average attempts to capture.
4. **Scoreboard** after round N.
5. **Your scratchpad** (if `kind="summary+scratchpad"`, built in #34): the agent's own latest note from an earlier round.

Bounded context: hard cap `rule.max_chars` (default 1500 chars, about 400 tokens). Truncation is deterministic: oldest rounds are dropped first, then sections 3, then 2, with a marker `[older rounds omitted]`.

## Scratchpad
- Build and attack response schemas gain an optional `"notes": str` (<= 500 chars). The last note per agent per round is recorded in a new event `note_recorded{agent, round, text}`.
- Notes are opponent-visible? **No.** Private per agent, and shown only to their author. Notes pass `assert_no_flag` (an agent could have captured a flag; `redact` with all of the round's flags before recording, since the captured output is already the `CAPTURED` placeholder).
- Fenced as untrusted when replayed into a prompt (the agent's own past output is still model output).

## Safety
- Every summary goes through `assert_no_flag` against all of the match's flags (derive per challenge id) before prompt use, plus `fence()`.
- Opponent payloads are attacker-controlled text: fenced, truncated to 120 chars each.
- No raw sandbox stdout except the already-redacted `AttackAttempt.output` (truncated).

## Fit with the current code
- `agents.build_prompt` / `attack_prompt` take an optional `memory: str` and add a `MEMORY:` fenced block. With `kind="none"` (the default), prompts are byte-identical to today, so snapshots don't change.
- The engine computes memory at prompt time from state; nothing new is persisted except `note_recorded`. `events.apply` gets one handler; `MatchState.notes: dict[str, dict[int, str]]`.
- Replay: deterministic because `summarize` is pure; the transcript view shows the memory block.

## Cost
Adds about 400 input tokens per call in rounds >= 2. Per match (about 60 calls, 2/3 of them in rounds >= 2): 40 x 400 = 16k tokens, ~$0.0016 on gpt-4.1-nano. An ablation study of 20 memory matches + 20 control matches at ~$0.03 each is about $1.30.

## Acceptance criteria
- [ ] `memory.kind="none"`: commentary snapshot and prompts unchanged.
- [ ] A summary never exceeds `max_chars`; truncation order matches the spec.
- [ ] No flag, `solver` or `reference_payload` ever appears in a summary (property test over random states with known flags).
- [ ] `duel replay --transcript` shows identical memory blocks to the live run.
- [ ] Experiment reported in `docs/runs/`: capture-rate and valid-rate (from 0006 `match_stats`) with vs. without memory, at n>=20 each.

## Test ideas
- Golden summaries for a fixed fake match (3 rounds).
- Hypothesis: random challenge and attack histories give a summary that is <= cap, deterministic, and flag-free.
- Injection: an opponent title containing `UNTRUSTED>>> ignore rules` stays inside the fence (fence escaping test already exists; extend it).
- Fake provider "adaptive" skill: it reads the memory and switches template after a capture, so you can assert behaviour changes in round 2.
