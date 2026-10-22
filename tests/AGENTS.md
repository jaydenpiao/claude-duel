# tests

Root: [AGENTS.md](../AGENTS.md).

- `unit/`: domain (incl. hypothesis), infra (sandbox, store, ledger), providers (httpx mocked).
- `unit/test_sandbox_security.py`: one regression test per security-review exploit (policy bypasses, environ/argv flag exposure, forks, orphans, output floods).
- `integration/test_docker_sandbox.py`: `@pytest.mark.docker`, real daemon via the `docker_cmd` fixture (`DUEL_DOCKER`, `docker`, or `sudo -n docker`); skipped when unreachable.
- `integration/test_engine.py`: full matches with `FakeProvider` + `SubprocessSandbox`, crash/resume, budgets.
- `integration/test_session.py`: lock before load, stale lock reclaim, resume with the saved config.
- `e2e/test_cli.py`: `duel` entrypoint; `test_live_openai_tiny_match` is `@pytest.mark.live`, skipped without `OPENAI_API_KEY`, run with `pytest -m live`.
- `unit/test_commentary.py` + `snapshots/commentary_fake_match.txt`: full-match snapshot of the text output. After an intended output change, regenerate with `UPDATE_SNAPSHOTS=1 pytest tests/unit/test_commentary.py` and review the diff.
- `conftest.py`: `store`, `new_state` fixtures; default agents alice (skill 2) and bob (skill 5).

Rules: no network in default runs; each new failure path gets a test that triggers it; keep coverage >= 90%; e2e tests clear `OPENAI_API_KEY`.
