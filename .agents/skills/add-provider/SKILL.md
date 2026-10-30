---
name: add-provider
description: Use when adding a new LLM provider behind the LLMProvider port. anthropic.py is the worked example.
---
1. Create `src/claude_duel/adapters/providers/<name>.py` with a class exposing `name: str` and `complete(CompletionRequest) -> Completion` (see `ports.py`). Use httpx, take key/model/timeout in `__init__`.
2. Map HTTP failures with the shared `http.py:raise_for_status(resp, "<name>")` (quota -> non-retryable, 429/5xx -> retryable with `retry_after`); handle only vendor-specific codes yourself. Never import from another vendor's adapter.
3. Add one `Vendor` entry to `registry.py:VENDORS` (model prefix, key env var, default model, factory). That is the only wiring: the container, config (`<name>_model`, `DUEL_<NAME>_MODEL`, the key env var) and `budget.vendor_of` read it. Do not touch `container.py`, `config.py` or `budget.py` for this.
4. Add only cheap models to `DEFAULT_PRICES` in `app/budget.py`. Premium models go in `PREMIUM` and stay opt-in via `allow_premium`. That is a price change, not wiring.
5. Tests in `tests/unit/test_providers.py` with mocked transport: success, malformed body, quota, retryable, timeout. Optional `@pytest.mark.live` test. Follow `anthropic.py`. Add the env var to the README table.
6. Update root AGENTS.md and adapters/AGENTS.md; add an ADR if the design changes.
