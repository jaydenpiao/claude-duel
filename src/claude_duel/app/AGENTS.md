# app

Use cases and wiring. Root: [AGENTS.md](../../../AGENTS.md).

- `engine.py`: `MatchEngine.run` loops `next_stage`; the caller holds the match lock; one method per stage (`_build`, `_validate`, `_attack`, `_score`). Each unit is skipped if already recorded; every change is `_checkpoint`ed: event appended to the log, applied via `domain/events.apply`, snapshot saved (ADR 0004). Attack attempts are checkpointed one by one. Errors move the match to `failed`.
- `agents.py`: `SYSTEM` prompt, `build_prompt`, `attack_prompt`, `parse_challenge`, `parse_attack`. Untrusted author text is wrapped by `fence()`. `parse` takes the first JSON object that validates; malformed output raises `MalformedOutputError`; build runs a repair loop: malformed or invalid answers (policy incl. hidden answer, solver run, reference run with its printed output, empty-input run, optional difficulty-gate probe `GATE`) are fed back up to `rules.max_format_retries` times, each a budgeted call; the rules in `build_rules` are generated from `domain/policy.py`; attack spends an attempt. Provider errors fail the stage (resumable) instead of forfeiting.
- `budget.py`: `DEFAULT_PRICES` (allowlist), `BudgetLimits`, `CostBudget.check` before each call and `charge` after.
- `replay.py`: recomputes scores from stored challenges/validations/attacks and compares to the last `round_scored` totals (`consistent`).
- `session.py`: `play` takes the match lock, then loads or creates (agents pinned to their effective model), then builds the engine from the match's saved agents and runs it. Resume always uses the saved agents, rules and limits and warns if config differs. An optional `EventObserver` (the CLI commentary or arena view) is fed existing events first, then each new one after it is durable, applied and saved, always through `observers.IsolatedObserver` (first failure is logged, counted as `observer.error` and disables it; it never raises into the engine). The CLI treats a closed stdout pipe as success.
- `container.py`: `make_provider`, `make_sandbox` (fails closed for real models), `build_store`, `pin_agents`, `build_engine(settings, metrics, store, agents)`. New adapters are wired here and nowhere else.

Gotchas
- Every prompt must pass `assert_no_flag`; attacker-visible output must go through `redact`.
- `Settings.model_for(agent)` is the one place models resolve: fake agents use `FAKE_MODEL`, others `agent.model` or `settings.openai_model`.
