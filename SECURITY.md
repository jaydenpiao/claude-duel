# Security policy

## Reporting
Report a vulnerability through GitHub's vulnerability reporting (Security tab → "Report a vulnerability"). While the repo is private that button is not available yet: message @jaydenpiao on GitHub instead and we will set up a private channel. Do not open a public issue, and never include a working flag, API key or match secret in a report. Match secrets live only in `<store>/<id>.secret` (0600); never copy those files out of the store.

## Threat model (summary)
Model-authored challenge code is untrusted. It is checked by an AST allowlist (`domain/policy.py`) and real-model matches run only in the Docker sandbox: no network, read-only rootfs, all capabilities dropped, non-root, pids/memory/CPU limits, flag over stdin only (ADR 0003). An author solver (never shown to the attacker) is held to the same policy and sandbox. Model output is also scanned for escape attempts, prompt injection, flag or result forgery and runner tampering. Hits are logged as findings for review and never auto-penalised (ADR 0008). The runner itself gives challenge code minimal builtins and stand-in modules, and the subprocess sandbox refuses anything but fake-provider templates. The subprocess sandbox is for the fake provider only. Details: [AGENTS.md](AGENTS.md#security-invariants), [docs/adr/0003-sandbox.md](docs/adr/0003-sandbox.md).

## Automated gates (`.github/workflows/security.yml`, every PR, main and weekly)
- ruff `S` (bandit rules) in CI lint
- `pip-audit --strict` on every installed dependency
- gitleaks over full history
- CodeQL `security-extended`, failing on any result (no upload: code scanning isn't enabled while the repo is private)
- Trivy on the built image (fixable HIGH/CRITICAL fail) and on the Dockerfile; the release job re-scans before pushing to GHCR
- Dependabot for pip, GitHub Actions and the Docker base image
- All actions pinned to commit SHAs; every workflow defaults to `permissions: {}` and grants per job

## Branch protection
GitHub branch protection and rulesets are **not active**: they need GitHub Pro or a public repository, and the API returns 403 for this private repo on the free plan. Nothing on GitHub stops a direct or force push to `main` today. Until that changes the rules are kept by process: every change lands through a PR, squash-merged by a merge script only when the PR is up to date with `main` and all seven checks (`check` on 3.11/3.12/3.13, `audit`, `codeql`, `image`, `secrets`) passed on its head commit; nobody force-pushes `main` or deletes it. CODEOWNERS is advisory for the same reason. When the repository goes public (or the plan changes), enable protection on `main` with those required checks, an up-to-date branch, linear history, code-owner review, no force pushes or deletion, and `enforce_admins` on.

## Image
Non-root (uid 10001), code root-owned, pip/setuptools removed. Run with `--read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --tmpfs /app/.duel:uid=10001,gid=10001,mode=0700` (staging does).
