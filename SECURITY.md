# Security policy

## Reporting
This repo is private. Report a vulnerability to the owner (@jaydenpiao) privately through a GitHub security advisory or a direct message. Do not open a public issue, and never include a working flag, API key or match secret in a report.

## Threat model (summary)
Model-authored challenge code is untrusted. It is checked by an AST allowlist (`domain/policy.py`) and real-model matches run only in the Docker sandbox: no network, read-only rootfs, all capabilities dropped, non-root, pids/memory/CPU limits, flag over stdin only (ADR 0003). An author solver (never shown to the attacker) is held to the same policy and sandbox. The runner itself gives challenge code minimal builtins and stand-in modules, and the subprocess sandbox refuses anything but fake-provider templates. The subprocess sandbox is for the fake provider only. Details: [AGENTS.md](AGENTS.md#security-invariants), [docs/adr/0003-sandbox.md](docs/adr/0003-sandbox.md).

## Automated gates (`.github/workflows/security.yml`, every PR, main and weekly)
- ruff `S` (bandit rules) in CI lint
- `pip-audit --strict` on every installed dependency
- gitleaks over full history
- CodeQL `security-extended`, failing on any result (no upload: code scanning isn't enabled on this private repo)
- Trivy on the built image (fixable HIGH/CRITICAL fail) and on the Dockerfile; the release job re-scans before pushing to GHCR
- Dependabot for pip, GitHub Actions and the Docker base image
- All actions pinned to commit SHAs; every workflow defaults to `permissions: {}` and grants per job

## Branch protection
`main` requires a PR with all CI and security checks green on an up-to-date branch, linear history, and blocks force pushes and deletion. Admin bypass is technically enabled (`enforce_admins` off) but not allowed by policy.

## Image
Non-root (uid 10001), code root-owned, pip/setuptools removed. Run with `--read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --tmpfs /app/.duel:uid=10001,gid=10001,mode=0700` (staging does).
