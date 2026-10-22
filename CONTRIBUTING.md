# Contributing

## Setup
```bash
uv venv && . .venv/bin/activate && uv pip install -e '.[dev]'
```

## Checks (all must pass)
```bash
ruff check . && ruff format --check . && mypy src && pytest
```
Coverage must stay at or above 90%. New failure handling needs a test that triggers it.

## Rules
- Domain code (`src/claude_duel/domain`) stays pure, with no I/O.
- New providers, sandboxes or stores go behind the Protocols in `ports.py` and are wired only in `app/container.py`.
- Never log, print or commit secrets. API keys come from env only.
- Only add low-cost models to the price table.

## Style for commits, branches and PRs
- Commits: short lowercase imperative, e.g. `add match state machine`, `fix sandbox timeout`. No long bodies unless essential, and no emoji.
- Branches: short kebab-case, e.g. `sandbox-limits`.
- PR titles: short, in the same style as commits.
- PR descriptions: 2 to 4 short bullets.
