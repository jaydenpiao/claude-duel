# tests

Root: [AGENTS.md](../AGENTS.md).

- `unit/`: domain (incl. hypothesis), infra (sandbox, store, ledger), providers (httpx mocked).
- `integration/test_engine.py`: full matches with `FakeProvider` + `SubprocessSandbox`, crash/resume, budgets.
- `e2e/test_cli.py`: `duel` entrypoint; `test_live_openai_tiny_match` is `@pytest.mark.live`, skipped without `OPENAI_API_KEY`, run with `pytest -m live`.
- `conftest.py`: `store`, `new_state` fixtures; default agents alice (skill 2) and bob (skill 5).

Rules: no network in default runs; each new failure path gets a test that triggers it; keep coverage >= 90%; e2e tests clear `OPENAI_API_KEY`.
