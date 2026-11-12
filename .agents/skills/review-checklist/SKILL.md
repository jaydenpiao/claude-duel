---
name: review-checklist
description: Use before opening or approving a PR; the definition of done plus security invariants.
---
Done
- [ ] `ruff check . && ruff format --check . && mypy src && python scripts/check_docs.py && vulture && pylint src tests && pytest` green; coverage >= 90%; CI green on 3.11-3.13.
- [ ] New behaviour and each new failure path have tests.
- [ ] No dead code, duplication, commented-out code or unlinked TODOs (gates enforce; never loosen them); domain has no I/O; adapters wired only in `app/container.py`.
- [ ] Docs updated? AGENTS.md, README, skills and ADRs touched by this change are updated in the same PR.
- [ ] Commit, branch and PR are short and lowercase; PR body 2-4 bullets.

Security
- [ ] No flag in logs, events, prompts (`assert_no_flag`) or unredacted attacker output (`redact`).
- [ ] No secrets printed, logged or committed; keys only from env.
- [ ] Sandbox limits and policy not loosened without review; host env not passed to the sandbox; flag never on argv/env.
- [ ] Heat similarity stays inside its pinned token and reference caps; legacy logs past that ceiling are unverified, not rescored with a different LCS. A penalty or a displayed similarity is never an upper bound; a pair not compared exactly is `heat unchecked` and charged 0. Full-heat admission must leave room in the work budget for every pair a legal round can admit.
- [ ] Real-model paths still fail closed without Docker.
- [ ] `security` workflow green; new actions pinned to a SHA with least-privilege `permissions`.
- [ ] Only allowlisted cheap models; budget caps checked before every call; quota errors not retried.
