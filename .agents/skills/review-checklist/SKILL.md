---
name: review-checklist
description: Use before opening or approving a PR; the definition of done plus security invariants.
---
Done
- [ ] `ruff check . && ruff format --check . && mypy src && pytest` green; coverage >= 90%; CI green on 3.11-3.13.
- [ ] New behaviour and each new failure path have tests.
- [ ] No duplicated logic; domain has no I/O; adapters wired only in `app/container.py`.
- [ ] Docs/AGENTS.md/ADR updated if behaviour or design changed.
- [ ] Commit, branch and PR are short and lowercase; PR body 2-4 bullets.

Security
- [ ] No flag in logs, events, prompts (`assert_no_flag`) or unredacted attacker output (`redact`).
- [ ] No secrets printed, logged or committed; keys only from env.
- [ ] Sandbox limits and policy not loosened without review; host env not passed to the sandbox.
- [ ] Only allowlisted cheap models; budget caps checked before every call; quota errors not retried.
