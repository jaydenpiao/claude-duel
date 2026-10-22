# Contributing

## Setup
```bash
uv venv && . .venv/bin/activate && uv pip install -e '.[dev]'
```

## Checks (all must pass)
```bash
ruff check . && ruff format --check . && mypy src && vulture && pylint src tests && pytest
```
Coverage must stay at or above 90%. CI also fails on complexity over 10, dead code, duplicated code, commented-out code and TODOs without an issue link (see AGENTS.md). New failure handling needs a test that triggers it.

CI runs on a self-hosted box runner; see "CI (self-hosted box runner)" in AGENTS.md.

## Rules
- Domain code (`src/claude_duel/domain`) stays pure, with no I/O.
- New providers, sandboxes or stores go behind the Protocols in `ports.py` and are wired only in `app/container.py`.
- Never log, print or commit secrets. API keys come from env only.
- Only add low-cost models to the price table.
- `CLAUDE.md` and `.cursor/skills` are symlinks; on Windows set `git config core.symlinks true` before cloning.

## Style for commits, branches and PRs
- Commits: short lowercase imperative, e.g. `add match state machine`, `fix sandbox timeout`. No long bodies unless essential, and no emoji.
- Branches: short kebab-case, e.g. `sandbox-limits`.
- PR titles: short, in the same style as commits.
- PR descriptions: 2 to 4 short bullets.
