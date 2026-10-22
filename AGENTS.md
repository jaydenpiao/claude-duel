# AGENTS.md

Start here. Agent-vs-agent staged CTF arena: two LLM agents each author Python challenges that hide a flag, the arena checks each is solvable, then each attacks the other's. CLI only (`duel`), no server.

## Architecture (hexagonal, ADR 0001)
- `src/claude_duel/domain/`: pure models, state machine, scoring, flag derivation, static policy. No I/O. See [domain/AGENTS.md](src/claude_duel/domain/AGENTS.md).
- `src/claude_duel/ports.py`: Protocols `LLMProvider`, `Sandbox`, `MatchStore`, `MatchLock`, `SpendLedger`, `Metrics`.
- `src/claude_duel/adapters/`: `providers/` (fake, openai, retry), `sandbox/` (subprocess, docker), `store/` (file store, hash-chained event log, spend ledger). See [adapters/AGENTS.md](src/claude_duel/adapters/AGENTS.md).
- `src/claude_duel/app/`: `engine.py` (runs stages), `agents.py` (prompts + parsing), `budget.py` (cost caps, model allowlist), `replay.py`, `container.py` (**composition root, the only place adapters are chosen**). See [app/AGENTS.md](src/claude_duel/app/AGENTS.md).
- `presentation/commentary.py`: `Commentary`, the default `duel run`/`resume` output. An `EventObserver` that folds events with `domain/events.apply` and prints header, round banners, plain-sentence lines, scoreboard, redacted snippets (`scrub`), winner and the `$X of $50 burned` line. Presentation only: never mutates the match. Snapshot: `tests/snapshots/`.
- `presentation/arena.py`: `ArenaView`, the live animated view (rich `Live`, 4 fps): HP-style score meters, a tug-of-war lead bar, per-round sparklines, a spinner on the current action, ✓ capture / 🛡 defense flashes, a 5-line ticker, the burn line and a winner banner. `frame(model, width)` is pure and snapshot-tested; it never wraps, so any width renders. Default for `duel run`/`resume` when stdout is a TTY; `--view arena|commentary` forces one, `--pace S` slows it for demos. Both renderers subclass `presentation/common.py:MatchFold` and share its helpers (`scrub`, `board`, `spend`, `result`, `attempt_view`, ...); put new shared text there, never copy it. NO_COLOR is honoured by rich.
- `config.py` (TOML then `DUEL_*` env), `observability.py` (logging, metrics), `cli.py` (`--format text|json`, `--view auto|arena|commentary`, `--pace`; text ends with `replay verified ✓`).

## Match lifecycle (`domain/state_machine.py`)
`pending -> build -> validate -> attack -> score -> (build | complete)`; any non-terminal stage can go to `failed`. `build` increments `round`. `score` goes to `complete` when `round >= rules.rounds`. `recover()` reopens a failed match at `failed_stage`; stage work is idempotent (skips ids already in `challenges`/`validations`/`attacks`). State is saved after every unit of work.

## Directory map
```
configs/{dev,staging,prod}.toml   per-env model + budget
docs/requirements.md docs/design.md docs/adr/000N-*.md
src/claude_duel/                  code (see above)
tests/{unit,integration,e2e}/     see tests/AGENTS.md
.github/workflows/{ci,security,staging,release}.yml  (+ dependabot.yml, SECURITY.md)
.agents/skills/                   task skills (mirrored at .cursor/skills)
```

## Setup, run, check
```bash
uv venv && . .venv/bin/activate && uv pip install -e '.[dev]'
duel --config configs/dev.toml run [--match-id ID] [--format json] [--view arena --pace 0.15]   # fake provider by default
duel resume ID ; duel replay ID [--transcript]
ruff check . && ruff format --check . && mypy src && vulture && pylint src tests && pytest
```
`pytest` excludes `live` tests and fails under 90% coverage (branch). `docker`-marked tests run against a real daemon and skip when none is reachable. CI runs Python 3.11, 3.12, 3.13.

## Environments and release (ADR 0005)
- dev/staging/prod differ only by config: all use `gpt-4.1-nano`; match cap $0.25/$0.50/$1.00, run cap $0.50/$1.00/$2.00, lifetime $40.
- PR: `ci.yml` + `security.yml` (pip-audit, gitleaks, CodeQL, Trivy image/config). Push to main: those + `staging.yml` (docker build, fake-provider smoke match with read-only rootfs and cap-drop ALL).
- Workflows: actions pinned to commit SHAs (Dependabot bumps them), top-level `permissions: {}` with per-job grants, `persist-credentials: false`. Keep it that way.
- Prod: push tag `vX.Y.Z` -> `release.yml` Trivy-scans, then pushes `ghcr.io/jaydenpiao/claude-duel:<tag>` + `latest` and creates a GitHub release. Only after the user approves. No hosted deploy yet.

## Cost rules
- Only low/medium models from `DEFAULT_PRICES` in `app/budget.py`; unknown models are rejected (`check_model_allowed`). Never add flagship models.
- Caps: per match (persisted in `state.usage`), per run (this process only), lifetime from `.duel/ledger.jsonl` (`$40`; `budget.key_limit_usd`, the key's $50 hard limit, must be >= it and is shown in output), re-read under `flock` before every call so concurrent runs share it. Every call is ledgered, including $0 fake calls. Quota errors abort, never retry.
- `OPENAI_API_KEY` comes from env only, held as `SecretStr`. Never print, log, commit or put it in CI without asking.

## Security invariants
- Flags are `HMAC(secret, challenge_id)`, derived on demand, passed to the sandbox only over stdin (runner header) and exposed to challenge code as the global `FLAG`; never on argv or in env. Never logged, never in events, never persisted (the match `secret` is, in the 0600 state file).
- Prompts are checked with `assert_no_flag`; sandbox output shown to attackers is `redact()`ed.
- Challenge source passes `domain/policy.py` (AST allowlist: pure modules, `sys.stdin`/`sys.stdout` only, module names only as `mod.attr`, no submodule access (`re.enum`, `json.codecs`), no `_` names, no introspection attributes (`gi_*`, `f_*`, `co_*`, `cr_*`, `tb_*`, `mro`), no strings containing `__`, no star/relative imports, banned builtins, size cap, no flag-shaped literals) before running. At runtime the runner gives challenge code only `policy.SAFE_BUILTINS`, an `__import__` limited to pure-module stand-ins (no submodules) and a `sys` stand-in with only `stdin`/`stdout`.
- Sandbox limits from `SandboxLimits`: 2s wall, 1s CPU, 128 MB, 4 KB output, 32 pids, 0.5 CPUs. Real-model matches run only in Docker and fail closed without it (ADR 0003); the subprocess sandbox is for the fake provider only and raises `SandboxRefusedError` for any source its `source_allowed` rejects (the container passes `fake.is_template`; only tests pass `tests/helpers.any_source`). Set `DUEL_DOCKER="sudo docker"` if the socket needs sudo.

## Coding standards
- No duplicated logic; shared helpers live once (e.g. `sandbox/common.py:execute`).
- `mypy --strict`, ruff rules in `pyproject.toml`, line length 120.
- CI quality gates: ruff `C90` (complexity <= 10), `ERA` (no commented-out code), `TD` (every TODO links an issue: `# TODO: ... https://github.com/jaydenpiao/claude-duel/issues/N`); `vulture` (dead code; framework names in `[tool.vulture] ignore_names` only); `pylint` duplicate-code (>= 6 similar lines). Fix the code, never raise a threshold or add `noqa` to pass.
- Dependency injection through constructors; wire only in `app/container.py`. Domain stays pure.
- Every failure path gets a test that triggers it.

## Git style
- `main` is protected: changes land only by PR, squash or rebase (linear history), with `check (3.11/3.12/3.13)`, `audit`, `secrets`, `codeql` and `image` green on an up-to-date branch. No force pushes or deletion. Never bypass it as admin.
- Commits: short lowercase imperative, e.g. `add match state machine`. Branches: kebab-case, e.g. `sandbox-limits`. PR title like a commit; body 2-4 short bullets. No emoji.

## Definition of done
All checks green locally and in CI, coverage >= 90%, new behaviour and failure cases tested, every AGENTS.md/README/skill/ADR the change affects updated in the same PR, no secrets or flags in output. Full list: [review-checklist](.agents/skills/review-checklist/SKILL.md).

## Skills
[add-provider](.agents/skills/add-provider/SKILL.md) · [add-challenge-type](.agents/skills/add-challenge-type/SKILL.md) · [run-smoke-match](.agents/skills/run-smoke-match/SKILL.md) · [cut-release](.agents/skills/cut-release/SKILL.md) · [debug-failed-match](.agents/skills/debug-failed-match/SKILL.md) · [review-checklist](.agents/skills/review-checklist/SKILL.md)
