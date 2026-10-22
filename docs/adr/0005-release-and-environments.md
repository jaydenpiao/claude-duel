# ADR 0005: Releases and environments
Status: accepted.
- Production = a versioned GitHub release (tag `vX.Y.Z`) plus a container image on GHCR (`ghcr.io/jaydenpiao/claude-duel`). No hosted deployment, no cloud accounts, no costs.
- Staging = the `staging` GitHub Environment, which builds the image and runs containerized smoke matches (fake provider) in Actions.
- Hosted deploy (e.g. Fly.io) is deferred until a web viewer/API exists. The seam is reserved: a future `adapters/deploy/` adapter behind a `Deployer` port, invoked only from the release workflow.
