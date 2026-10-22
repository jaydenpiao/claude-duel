# app

Use cases and wiring. Root: [AGENTS.md](../../../AGENTS.md).

- `engine.py`: `MatchEngine.run` loops `next_stage` under the match lock; one method per stage (`_build`, `_validate`, `_attack`, `_score`). Each unit is skipped if already recorded, then checkpointed (event + save). Errors move the match to `failed`.
- `agents.py`: `SYSTEM` prompt, `build_prompt`, `attack_prompt`, `parse_challenge`, `parse_attack`. Malformed output raises `MalformedOutputError`; build retries up to `rules.max_format_retries`, attack spends an attempt.
- `budget.py`: `DEFAULT_PRICES` (allowlist), `BudgetLimits`, `CostBudget.check` before each call and `charge` after.
- `replay.py`: recomputes scores from stored challenges/validations/attacks and compares to the last `round_scored` totals (`consistent`).
- `container.py`: `make_provider`, `make_sandbox`, `build_engine`. New adapters are wired here and nowhere else.

Gotchas
- Every prompt must pass `assert_no_flag`; attacker-visible output must go through `redact`.
- `model_for` maps the placeholder model `fake-1` to `settings.openai_model` for openai agents.
