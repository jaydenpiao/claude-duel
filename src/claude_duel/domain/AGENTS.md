# domain

Pure rules. No I/O, no adapters, no logging. Root context: [AGENTS.md](../../../AGENTS.md).

- `models.py`: `Stage`, `AgentSpec`, `Rules`, `SandboxLimits`, `ChallengeSpec`, `Challenge`, `Validation`, `Attack`, `Usage`, `Event`, `MatchState` (the only mutable aggregate).
- `state_machine.py`: `_ALLOWED` transitions, `next_stage`, `transition`, `recover`.
- `flags.py`: `derive_flag`, `assert_no_flag`, `redact`, `FLAG_PATTERN` (`DUEL{hex}`).
- `policy.py`: static AST check of challenge source.
- `scoring.py`: `score_round`, `add_scores` (deterministic, replay depends on it).
- `errors.py`: `DuelError` hierarchy; CLI maps any `DuelError` to exit 2.

Gotchas
- Value models are frozen with `extra="forbid"`; adding a field to `MatchState` breaks loading old state files unless it has a default.
- Changing scoring changes replay results for old matches; replay recomputes scores from stored validations and attacks and compares them to the last `round_scored` event.
- Loosening `policy.py` widens what runs in the sandbox; needs a security review.
