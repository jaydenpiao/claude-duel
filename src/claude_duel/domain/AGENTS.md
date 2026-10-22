# domain

Pure rules. No I/O (`policy._is_submodule` only imports stdlib pure modules to inspect them), no adapters, no logging. Root context: [AGENTS.md](../../../AGENTS.md).

- `models.py`: `Stage`, `AgentSpec`, `Rules`, `SandboxLimits`, `ChallengeSpec`, `Challenge`, `Validation`, `Attack`, `Usage`, `Event`, `MatchState` (the only mutable aggregate).
- `state_machine.py`: `_ALLOWED` transitions, `next_stage`, `transition`, `recover`.
- `flags.py`: `derive_flag`, `assert_no_flag`, `redact`, `scrub` (the only redactor), `FLAG_PATTERN` (`DUEL{hex}`).
- `policy.py`: static AST check of challenge (and solver) source, plus `answer_exposed` (hidden-answer rule, ADR 0006).
- `events.py`: event types, one typed payload model per type, `SCHEMA_VERSION`, `payload()` validation, `apply`/`fold` (ADR 0004). `attack_started` (key, try_no) is emitted before each attack call for presentation; its handler changes no state, so logs written before it existed replay identically.
- `modes.py`: `MODES` presets (plain `Rules`: classic, quick, gated, checked); `config.load_settings` applies `[rules] mode` / `DUEL_MODE` first, then the config's own fields.
- `contract.py`: the `check` challenge form (`compose`, `WRAPPER`, `solver_program`; ADR 0010).
- `findings.py`: tamper detector (`scan_build`, `scan_payload`); logged as `finding_flagged`, never scored (ADR 0008). `policy.reason_code` gives rejection reasons a machine code.
- `scoring.py`: `score_round` builds `RoundFacts` and dispatches to `SCORERS[rules.scoring.kind]` (only `classic` today, ADR 0007); `solve_award`/`defense_award` are the shared base new scorers compose; `add_scores` (deterministic, replay depends on it). Every scorer must pass `TestScorerInvariants`. `match_stats` gives valid/capture rates for run reports.
- `memory.py`: between-round memory (#19): a pure, bounded, flag-free summary of past rounds built from event-sourced state (own outcomes, opponent challenges and tendencies, scoreboard); never `solver` or `reference_payload`. Kind `summary+scratchpad` (#34) adds the agent's own last note: replies may carry an optional `"notes"` (<= 500 chars), recorded as `note_recorded{agent, round, text}` after redaction against every flag of the match, folded into `MatchState.notes`, and shown only to its author.
- `errors.py`: `DuelError` hierarchy; CLI maps any `DuelError` to exit 2.

Gotchas
- Value models are frozen with `extra="forbid"`; adding a field to `MatchState` breaks loading old state files unless it has a default.
- Changing scoring changes replay results for old matches; replay folds the event log (`events.fold`), recomputes each `round_scored` delta and diffs the folded state against the snapshot. `events.py` holds the event types and `apply`, the only place match state changes; add a handler there for any new event type.
- Loosening `policy.py` widens what runs in the sandbox; needs a security review.
