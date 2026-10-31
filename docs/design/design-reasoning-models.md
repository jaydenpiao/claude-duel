# Design: running the duel on a reasoning model (gpt-5-nano default)
Status: draft for Elon, design check 2026-10-29 (11:05 AM PDT). Design only, no repo code changed.
Trigger: [PR 187](https://github.com/jaydenpiao/claude-duel/pull/187) made `gpt-5-nano` the default `openai_model` and `gate_model`.

## Conflict found
- `adapters/providers/openai.py:25`: `RESPONSES_MODELS = ("gpt-6", "gpt-5.6")`. `gpt-5-nano` does not match, so it goes through Chat Completions (`_chat_body`) with `temperature = request.temperature` (default `0.0`, `ports.py:21`).
- gpt-5-family models are reasoning models and accept only the default temperature; a request with `temperature: 0` is rejected with a 400 ([OpenAI reasoning guide](https://platform.openai.com/docs/guides/reasoning)). Hunch to verify with one $0 fake-transport test and one real call: every live call on the new default fails before any play.
- `_parse_chat` ignores `usage.completion_tokens_details.reasoning_tokens`, so `n_tokens` stays 0 on this path. Reasoning tokens are billed as output and sit inside `max_completion_tokens`; at default effort a short cap can be eaten by reasoning and come back empty with `finish_reason == "length"` (`truncated`).
- `test_default_model.py` only checks config values, so CI is green while live play is broken.

## Why it matters for the game designs
- **Fairness / token clock / wallets / ledger stakes**: all fold `output_tokens`. Hidden reasoning means a side's thinking is charged but not shown (`think 0` in the footer) and the efficiency mode can't tell "thought a lot" from "wrote a lot". Cross-vendor pairings (Anthropic path already reports thinking) become unequal in what is recorded.
- **Determinism**: replay is event-sourced (ADR 0004), so it stays deterministic; but temperature 0 no longer exists, so experiment variance rises. The stakes prereg must not assume temperature 0.
- **Cost**: `duel estimate` bounds output by `max_output_tokens`, which is still a correct worst case, but typical cost per call rises with reasoning. Pin effort low.

## Proposal (small, $0 to build)
1. Route the gpt-5 family through Responses: add `"gpt-5"` to `RESPONSES_MODELS` (covers gpt-5-nano/mini and the existing gpt-5.6 prefix). Responses already drops temperature and sends `reasoning.effort`.
2. Default `rules.reasoning_effort` (or per-agent) to `"minimal"` for gpt-5-nano so reasoning cannot starve the answer; record the effort in `match_created` (already in the event as `reasoning_effort`).
3. Parse reasoning tokens on both OpenAI paths (`output_tokens_details.reasoning_tokens` on Responses, `completion_tokens_details.reasoning_tokens` on Chat) into `n_tokens`.
4. Keep Chat Completions for non-reasoning models only; if a model is unknown, fail closed in config validation rather than guess.
5. Note in the stakes prereg and README: gpt-5-nano is itself deprecated with shutdown planned 2026-12-11 (design-core-logic.md §0), so the launch must finish before then or swap again.

## Acceptance criteria
- [ ] Fake-transport test: with the default settings, the request body sent for `gpt-5-nano` has no `temperature` key and goes to `/responses`.
- [ ] Parse tests: a Responses and a Chat body with reasoning token details produce `n_tokens > 0` and `n_tokens <= output_tokens`.
- [ ] Truncation test: reasoning that fills the cap with empty text is `truncated`, and the engine's existing shorter-answer retry path runs once.
- [ ] `duel estimate --model gpt-5-nano` worst case stays <= `match_max_usd` ($0.25) for every preset.
- [ ] One real smoke call (< $0.001) on gpt-5-nano succeeds before any launch or stakes spend.
- [ ] Classic scoring snapshots unchanged.
