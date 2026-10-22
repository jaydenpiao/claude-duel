---
name: cut-release
description: Use when shipping a production version (tag, image, GitHub release) after user approval.
---
1. Get explicit user approval for prod.
2. Confirm main is green: latest `ci`, `security` and `staging` runs passed (`gh run list --branch main`).
3. Pick semver `vX.Y.Z`; bump `version` in `pyproject.toml` via a small PR (`bump version to X.Y.Z`).
4. `git tag vX.Y.Z && git push origin vX.Y.Z` -> `release.yml` Trivy-scans the image (fixable HIGH/CRITICAL abort the release), then pushes `ghcr.io/jaydenpiao/claude-duel:vX.Y.Z` and `:latest` and runs `gh release create --generate-notes` (the changelog).
5. Verify with `gh release view vX.Y.Z` and the workflow run.
6. Rollback: pull/retag the previous image (`docker pull ghcr.io/jaydenpiao/claude-duel:vPREV`), and if needed retag it as `latest`; fix forward with a new patch version. Never move or delete an existing tag.
