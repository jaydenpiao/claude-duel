# Requirements

## Functional
- FR1 A match pits two agents against each other over N rounds.
- FR2 Each round has four stages: Build, Validate, Attack, Score.
- FR3 Build: each agent authors `challenges_per_round` CTF challenges (description, Python source, reference payload, difficulty 1-3). The arena injects a per-challenge secret flag as the global `FLAG` at run time (sent over stdin, never argv or env); authors never see it.
- FR4 Validate: a challenge is valid only if it passes static policy (size, banned capabilities, no flag-shaped literals, and the accepting input not visible in the source in any obvious encoding), the reference payload (or the output of the author's hidden `solver` program) yields the flag, and an empty payload does not. Invalid challenges cost the author points.
- FR5 Attack: each agent attacks every valid opponent challenge within step (attempt) and token budgets. Previous attempt outputs are fed back.
- FR6 Score: deterministic, pure scoring; difficulty and round multipliers escalate stakes each round. A defense is worth at least as much as a capture (ADR 0006). An optional difficulty gate (`rules.difficulty_gate`, cheap `gate_model`) sends challenges solved by one probe attempt back to repair.
- FR7 Every decision is appended to an event log; `duel replay` recomputes scores from the log and must match.
- FR8 CLI: `duel run`, `duel resume`, `duel replay`.
- FR9 Providers: deterministic fake (default/tests), Anthropic, OpenAI (enabled when API key env var is set).

## Non-functional
- Python 3.11+, typed (mypy strict), ruff clean, >=90% coverage.
- Hexagonal architecture; adapters swappable via DI.
- Idempotent, resumable matches: state checkpointed after every unit of work.
- Structured JSON logs with trace IDs; metrics hook; secrets and flags redacted.
- Config from TOML file with `DUEL_*` env overrides.

## Failure modes
| Failure | Handling |
|---|---|
| Provider 5xx / transport error | Retry with exponential backoff + full jitter, bounded attempts |
| Provider timeout | Engine retries up to 2 more times, no backoff, each try under a fresh reservation kept at its worst case (likely billed) |
| Rate limit (429) | Honour `retry-after`, else backoff |
| Permanent provider error (4xx) | No retry; unit forfeited, match continues |
| Malformed or invalid model output | Re-prompt with the parse or validation error up to `max_format_retries` (budgeted); then forfeit/invalid (build) or wasted attempt (attack) |
| Sandbox escape attempt | AST allowlist; real-model matches require Docker (fail closed); no network, read-only, non-root; rlimits for the fake-only subprocess sandbox |
| Infinite loop / memory bomb | CPU, address-space, file-size, wall-clock limits; output truncated |
| Flag leakage | Flags derived via HMAC, never in prompts (guarded), redacted from logs and attack feedback |
| Crash mid-match | Atomic checkpoint per unit; `duel resume` continues; completed units skipped |
| Concurrent runs on one match | `flock` per match held across load, recover and run; released by the kernel on crash |
