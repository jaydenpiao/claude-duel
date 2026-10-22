# app

Use cases and wiring. Root: [AGENTS.md](../../../AGENTS.md).

- `engine.py`: `MatchEngine.run` loops `next_stage` under the match lock; one method per stage (`_build`, `_validate`, `_attack`, `_score`). Each unit is skipped if already recorded; every change is `_checkpoint`ed: event appended to the log, applied via `domain/events.apply`, snapshot saved (ADR 0004). Attack attempts are checkpointed one by one. Errors move the match to `failed`.
- `agents.py`: `SYSTEM` prompt, `build_prompt`, `attack_prompt`, `parse_challenge`, `parse_attack`. Untrusted author text is wrapped by `fence()`. `parse` takes the first JSON object that validates; malformed output raises `MalformedOutputError`; build retries up to `rules.max_format_retries`, attack spends an attempt. Provider errors fail the stage (resumable) instead of forfeiting.
- `budget.py`: `DEFAULT_PRICES` (allowlist), `BudgetLimits`, `CostBudget.check` before each call and `charge` after.
- `replay.py`: recomputes scores from stored challenges/validations/attacks and compares to the last `round_scored` totals (`consistent`).
- `container.py`: `make_provider`, `make_sandbox` (fails closed for real models), `build_store`, `build_engine`. New adapters are wired here and nowhere else.

Gotchas
- Every prompt must pass `assert_no_flag`; attacker-visible output must go through `redact`.
- `Settings.model_for(agent)` is the one place models resolve: fake agents use `FAKE_MODEL`, others `agent.model` or `settings.openai_model`.
