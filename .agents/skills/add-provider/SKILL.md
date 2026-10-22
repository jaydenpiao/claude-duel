---
name: add-provider
description: Use when adding a new LLM provider (e.g. anthropic) behind the LLMProvider port.
---
1. Create `src/claude_duel/adapters/providers/<name>.py` with a class exposing `name: str` and `complete(CompletionRequest) -> Completion` (see `ports.py`). Use httpx, take key/model/timeout in `__init__`.
2. Map HTTP failures to domain errors like `openai.py:raise_for_status` (quota -> non-retryable, 429/5xx -> retryable with `retry_after`).
3. Wire it in `app/container.py:make_provider`, wrapped in `RetryingProvider`. Key from env only as `SecretStr` in `config.py` (mirror `OPENAI_API_KEY`); missing key -> `ConfigError`.
4. Add only cheap models to `DEFAULT_PRICES` in `app/budget.py`.
5. Tests in `tests/unit/test_providers.py` with mocked transport: success, malformed body, quota, retryable, timeout. Optional `@pytest.mark.live` test.
6. Update root AGENTS.md and adapters/AGENTS.md; add an ADR if the design changes.
