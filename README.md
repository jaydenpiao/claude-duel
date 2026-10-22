# claude-duel

![A fake-provider match in the live arena view: score bars, tug-of-war lead bar, captures and defenses, winner banner](docs/demo.gif)

Two AI agents duel in stages. Each one builds CTF challenges for the other, the arena checks every challenge is fair, then each tries to break the other's. Scoring is deterministic and every match can be replayed.

(Named as a joke on Claude Duet. The idea came from "there should be a claude duel".)

## How a round works
1. **Build**: each agent writes `challenges_per_round` Python programs that print the global `FLAG` only for the right stdin, along with a reference payload.
2. **Validate**: static AST allowlist (pure imports, `sys.stdin`/`sys.stdout` only, no `_` names, size, flag literals), then a sandbox run: the reference payload must reveal the flag and empty input must not. Invalid challenges cost points.
3. **Attack**: the opponent sees the source and description and gets `max_attempts` tries within a token budget, with feedback after each try.
4. **Score**: a pure function. Points scale with difficulty × round, so stakes rise each round.

## Quick start
```bash
uv venv && . .venv/bin/activate && uv pip install -e '.[dev]'
duel run --match-id demo            # fake provider, no network, free; live arena view on a terminal
duel run --view arena --pace 0.15   # animated scoreboard, slowed down to watch (or --view commentary)
duel run --format json              # one JSON summary instead (scripts, CI)
duel card demo [--out card.svg]     # shareable SVG card: models, score, rounds, spend, replay-verified log hash
duel replay demo --transcript       # refold the event log and verify scores
duel series --games 3                # best-of-3: one normal match per game, sides alternate, then verified
duel ratings                        # offline Elo + Bradley-Terry (95% CI) over every completed match
duel estimate [--mode capped]       # offline worst-case $ per match for each preset (no API calls)
duel resume demo                    # continue a crashed or aborted match
OPENAI_API_KEY=... duel --config configs/dev.toml run   # real models (see below)
```

To use OpenAI, set `provider = "openai"` on the agents in a config file, as in `tests/e2e/test_cli.py`. Only low-cost models from the price table are allowed (default `gpt-4.1-nano`).

## Architecture
```mermaid
flowchart LR
  CLI[cli.py] --> C[container.py<br/>composition root]
  C --> E[MatchEngine]
  E --> SM[state machine]
  E --> SC[scoring]
  E --> PO[policy + flags]
  E --> B[CostBudget]
  E -->|LLMProvider| P{{providers}}
  P --> F[Fake]
  P --> R[Retrying] --> O[OpenAI]
  E -->|Sandbox| S{{sandboxes}}
  S --> SP[subprocess + rlimits]
  S --> D[Docker no-network]
  E -->|MatchStore| ST[(atomic JSON + lock)]
  B -->|SpendLedger| L[(ledger.jsonl)]
```
Lifecycle: `pending → build → validate → attack → score → (build | complete)`, and any stage can go to `failed`, which `resume` recovers from. See [docs/design.md](docs/design.md), [requirements](docs/requirements.md) and the [ADRs](docs/adr).

## Cost controls
- Code-defined allowlist of low-cost models (`app/budget.py:DEFAULT_PRICES`); config cannot add models or change prices.
- Per-match caps (calls, tokens, USD; $0.25 dev / $0.50 staging / $1.00 prod), per-run caps, and a persistent ledger (`.duel/ledger.jsonl`) with a $40 lifetime ceiling. Once that ceiling is hit, new work is refused.
- A provider `insufficient_quota` error is never retried. The match fails cleanly and can be resumed later.

## Threat model
| Threat | Mitigation |
|---|---|
| Challenge code escapes the sandbox (network, files, processes) | AST allowlist; flag and source sent over stdin (never argv or environ); real-model matches only run in Docker (`--network none`, read-only rootfs, cap-drop ALL, no-new-privileges, seccomp, non-root, pids/memory/CPU limits, container removed on timeout) and fail closed if the daemon is unreachable; subprocess sandbox (fake provider only) adds rlimits incl. no forks and kills the whole process group |
| Infinite loops and memory bombs | CPU + wall-clock + memory limits; output truncated |
| Flag leakage to the attacker | Flags are HMAC-derived by the arena and never shown to authors; prompts are guarded (`FlagLeakError`); feedback and logs are redacted; flags are never persisted |
| Trivial or unsolvable challenges | Reference payload must succeed and empty input must fail |
| Prompt injection inside challenge text | The attacker only produces stdin. Model output is never executed by the arena, only parsed as JSON |
| Secrets in logs | Keys only come from env and are held as `SecretStr`; the JSON log formatter scrubs keys and flags |
| Score tampering | hash-chained event log; `duel replay` refolds it, rescores and flags mismatches |
| Runaway spend | Budget circuit breaker + ledger ceiling |

**Modes:** `[rules] mode = "quick"` (or `DUEL_MODE=quick`) starts from a preset in `domain/modes.py` (`classic`, `quick`, `gated`, `checked`: authors write only `def check(s)`, with seeded themes and no repeat designs, ADR 0010; `efficiency`: captures are discounted by the tokens they took; `capped`: each agent has a 30k-token wallet, after which its builds forfeit and attacks stop; `blitz`: a 40k-token clock per agent with a 500-token increment per call, and slow captures are worth less); any other `[rules]` field overrides it.

**Environment overrides** (applied over the TOML file; a full key list is enforced by `tests/unit/test_env_docs.py`):

| Variable | Setting | Default |
|---|---|---|
| `DUEL_SANDBOX` | `sandbox` (`auto`/`docker`/`subprocess`) | `auto` |
| `DUEL_STORE_DIR` | `store_dir` | `.duel` |
| `DUEL_LOG_LEVEL` | `log_level` | `INFO` |
| `DUEL_OPENAI_MODEL` | `openai_model` | `gpt-4.1-nano` |
| `DUEL_GATE_MODEL` | `gate_model` (difficulty-gate probe) | `gpt-4.1-nano` |
| `DUEL_PROVIDER_TIMEOUT` | `provider_timeout` seconds | 60 |
| `DUEL_PROVIDER_MAX_RETRIES` | `provider_max_retries` | 4 |
| `DUEL_ROUNDS` | `[rules] rounds` | preset |
| `DUEL_MAX_ATTEMPTS` | `[rules] max_attempts` | preset |
| `DUEL_CHALLENGES_PER_ROUND` | `[rules] challenges_per_round` | preset |
| `DUEL_MODE` | `[rules] mode` (`classic`/`quick`/`gated`) | `classic` |
| `DUEL_TOKEN_BUDGET` | `[rules] attack_token_budget` | preset |
| `DUEL_MATCH_MAX_USD` | `[budget] match_max_usd` | 0.25 |
| `DUEL_RUN_MAX_USD` | `[budget] run_max_usd` | 0.50 |
| `DUEL_MATCH_MAX_CALLS` | `[budget] match_max_calls` | 80 |
| `DUEL_MATCH_MAX_TOKENS` | `[budget] match_max_tokens` | 150000 |
| `DUEL_RUN_MAX_CALLS` | `[budget] run_max_calls` | 200 |
| `DUEL_LIFETIME_MAX_USD` | `[budget] lifetime_max_usd` (ledger ceiling, must stay <= the $50 key limit) | 40 |
| `DUEL_DOCKER` | docker command, e.g. `"sudo docker"` | `docker` |

`OPENAI_API_KEY` is read from the environment only.

**Allowed models** (`app/budget.py:DEFAULT_PRICES`, USD per 1M input/output tokens; any other model is refused unless a config adds it to `prices`):
`gpt-5-nano` (0.05/0.40), `gpt-4.1-nano` (0.10/0.40), `gpt-4o-mini` (0.15/0.60), `gpt-5-mini` (0.25/2.00),
`gpt-4.1-mini` (0.40/1.60).

**Caveat:** the subprocess sandbox is defence in depth, not a security boundary, so the arena refuses to run real-model matches on it. Set `DUEL_DOCKER="sudo docker"` if your user cannot reach the Docker socket.

## Development
`ruff check . && ruff format --check . && mypy src && pytest`. Live test: `pytest -m live` (needs `OPENAI_API_KEY`; costs well under $0.01).

Releases and environments: [ADR 0005](docs/adr/0005-release-and-environments.md).

The hero GIF is rendered from `docs/demo.tape` (`vhs docs/demo.tape`, fake provider, $0; keep it under 2 MB).
