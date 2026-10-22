# Requirements

## Functional
- FR1 A match pits two agents against each other over N rounds.
- FR2 Each round has four stages: Build, Validate, Attack, Score.
- FR3 Build: each agent authors `challenges_per_round` CTF challenges (description, Python source, reference payload, difficulty 1-3). The arena injects a per-challenge secret flag as the global `FLAG` at run time (sent over stdin, never argv or env); authors never see it.
- FR4 Validate: a challenge is valid only if it passes static policy (size, banned capabilities, no flag-shaped literals), the reference payload yields the flag, and an empty payload does not. Invalid challenges cost the author points.
- FR5 Attack: each agent attacks every valid opponent challenge within step (attempt) and token budgets. Previous attempt outputs are fed back.
- FR6 Score: deterministic, pure scoring; difficulty and round multipliers escalate stakes each round.
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
| Provider timeout / 5xx | Retry with exponential backoff + full jitter, bounded attempts |
| Rate limit (429) | Honour `retry-after`, else backoff |
| Permanent provider error (4xx) | No retry; unit forfeited, match continues |
| Malformed model output | Re-prompt with parse error up to `max_format_retries`; then forfeit (build) or wasted attempt (attack) |
| Sandbox escape attempt | AST allowlist; real-model matches require Docker (fail closed); no network, read-only, non-root; rlimits for the fake-only subprocess sandbox |
| Infinite loop / memory bomb | CPU, address-space, file-size, wall-clock limits; output truncated |
| Flag leakage | Flags derived via HMAC, never in prompts (guarded), redacted from logs and attack feedback |
| Crash mid-match | Atomic checkpoint per unit; `duel resume` continues; completed units skipped |
| Concurrent runs on one match | Lock file per match |
