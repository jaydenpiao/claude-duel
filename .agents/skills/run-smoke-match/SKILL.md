---
name: run-smoke-match
description: Use when you need to run a match end to end, offline or against real OpenAI with a tiny cost cap.
---
Offline (free): `duel --config configs/dev.toml run --match-id smoke` (fake agents).
Containerized like staging: `docker build -t claude-duel:staging . && docker run --rm claude-duel:staging --config configs/staging.toml run --match-id smoke`.

Live OpenAI (only when asked). Real models only run in Docker; set `DUEL_SANDBOX=docker` (and `DUEL_DOCKER="sudo docker"` if needed):
1. Check `OPENAI_API_KEY` is set with `test -n "$OPENAI_API_KEY" && echo set`. Never echo, print or log the value.
2. Write a temp TOML with both agents `provider = "openai"`, `model = "gpt-4.1-nano"` (or another `DEFAULT_PRICES` model), `rounds = 1`, `challenges_per_round = 1`, and `[budget] match_max_usd = 0.05`.
3. Or run `pytest -m live` (the built-in tiny match, capped at $0.05).
4. Report `est_usd` from the summary line and `ledger.jsonl` total. Lifetime ceiling is $40.
