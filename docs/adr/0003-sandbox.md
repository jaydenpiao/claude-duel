# ADR 0003: Sandbox behind a port
Status: accepted (revised 2026-10-21 after security review round 1).

- Real-model matches run only in the Docker sandbox. `make_sandbox` fails closed with `ConfigError` if any agent is not `fake` and Docker is not usable; `sandbox="auto"` falls back to subprocess only for fake-only matches. `DockerSandbox.health()` checks that the daemon answers `docker info` with an active seccomp profile, then runs a real self-test container; a broken daemon or image is never used. Fake-only `auto` fallback logs a warning.
- Docker run: `--network none`, `--read-only` with a 1 MB noexec `/tmp`, `--cap-drop ALL`, `no-new-privileges`, default seccomp, `--ipc none`, user 65534, `--pids-limit`/`--memory` (= swap)/`--cpus`/`--ulimit cpu` from `SandboxLimits`, `--log-driver none`, a unique `--name`, and `docker rm -f` after every run. The wall limit is enforced inside the container by `timeout`; the host adds `STARTUP_GRACE_SECONDS` (5s) only as a backstop for container start-up.
- Flag and source travel over stdin to a shared runner (`adapters/sandbox/runner.py`), never argv or env.
- The subprocess sandbox is defence in depth only (fake provider, tests): rlimits incl. `RLIMIT_NPROC=1` (no forks), empty read-only cwd, clean env, process-group kill after every run.
- Challenge code must also pass the AST allowlist in `domain/policy.py`.
