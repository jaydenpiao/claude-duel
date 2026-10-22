# Design

## Layers
- `domain/` pure: models, state machine, scoring, policy, errors. No I/O.
- `ports.py` Protocols: `LLMProvider`, `Sandbox`, `MatchStore`, `Metrics`.
- `adapters/` providers (fake, anthropic, openai, retry decorator), sandbox (subprocess+rlimits, docker), store (atomic JSON files).
- `app/` `MatchEngine` orchestrates stages; `agents.py` prompts and output parsing; `container.py` wires dependencies.
- `cli.py` thin argparse front end.

## Lifecycle
`PENDING -> BUILD -> VALIDATE -> ATTACK -> SCORE -> (BUILD next round | COMPLETE)`; any stage may go to `FAILED`. Transitions are enforced by `domain/state_machine.py`.

## Idempotency
Each unit has a key (`build:r1:alice:0`, `attack:r1:bob:<challenge>`). The engine skips units already recorded and checkpoints after each one, so resume never repeats paid model calls for finished work. Every change is an event in the hash-chained `<id>.events.jsonl` (ADR 0004), including each attack attempt, so a crash mid-attack resumes at the next attempt.

## Flags
`flag = DUEL{hmac_sha256(match_secret, challenge_id)[:32]}`. The secret is generated per match and stored only in the state file (never logged).

## Scoring (per round r, multiplier m=r)
- scorer `rules.scoring.kind` (registry `scoring.SCORERS`, ADR 0007); `classic`:
- invalid/forfeited challenge: author `-invalid_penalty*m`
- solved: attacker `+solve_points*difficulty*m - (attempts-1)`, floored at 1
- valid and unsolved: author `+defense_points*difficulty*m` (defense_points >= solve_points, ADR 0006)

See ADRs in `docs/adr/`.
