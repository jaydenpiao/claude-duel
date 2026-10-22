# ADR 0011: optional gVisor (runsc) runtime

Status: accepted (closes #43).

## Context
The Docker sandbox (ADR 0003) shares the host kernel. gVisor's `runsc` runs each container on a
user-space kernel, which puts a second boundary in front of the host kernel. It does not need
KVM, unlike Firecracker.

## Decision
- `docker_runtime` (`DUEL_DOCKER_RUNTIME`, for example `runsc`) adds `--runtime <name>` to the
  hardened `docker run` argv. All the other flags stay the same. The default is unset, which
  uses the daemon default (runc).
- When the runtime isn't registered with the daemon (`docker info` Runtimes), the arena logs a
  warning and falls back to the default runtime. The normal health self-test still gates the
  sandbox, so real-model matches still fail closed when Docker itself is broken.
- The name must match `^[a-z][a-z0-9_.-]*$`, so it can't inject extra flags.

## Consequences
- The host pids cgroup also counts gVisor's own threads. runsc did not start at `pids_limit` 16
  and ran out of memory at 24; the default of 32 works. A fork bomb still fails closed: the
  container dies and no flag leaks.
- CI has no runsc, so `test_runsc_runtime_runs_hardened` skips there. It runs on hosts that
  have runsc registered.
- Setup (from gvisor.dev): extract the release tarball into `/usr/local/bin`, then run
  `sudo runsc install` and reload dockerd.
