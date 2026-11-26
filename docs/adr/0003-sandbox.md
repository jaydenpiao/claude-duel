# ADR 0003: Sandbox behind a port
Status: accepted (revised 2026-10-21 after security review round 1).

- Real-model matches run only in the Docker sandbox. `make_sandbox` fails closed with `ConfigError` if any agent is not `fake` and Docker is not usable; `sandbox="auto"` falls back to subprocess only for fake-only matches. `DockerSandbox.health()` checks that the daemon answers `docker info` with an active seccomp profile, then runs a real self-test container; a broken daemon or image is never used. Fake-only `auto` fallback logs a warning.
- Docker run: `--network none`, `--read-only` with a 1 MB noexec `/tmp`, `--cap-drop ALL`, `no-new-privileges`, Docker's default seccomp profile (moby v26.1.5) minus `ptrace`, `process_vm_readv`/`writev` and `personality` (`adapters/sandbox/seccomp.json`, S16-1: a same-uid ptrace could stop the in-container `timeout`), `--ipc none`, user 65534, `--pids-limit`/`--memory` (= swap)/`--cpus`/`--ulimit cpu` from `SandboxLimits`, `--log-driver none`, a unique `--name`, and `docker rm -f` after every run. The wall limit is enforced inside the container by `timeout`; the host adds `STARTUP_GRACE_SECONDS` (5s) only as a backstop for container start-up. On a host deadline the container is removed (`docker rm -f`) before the reader is joined, and every reap wait is bounded by `REAP_SECONDS` (5s), so a stopped watchdog or a surviving privileged docker client cannot hang a match.
- Flag and source travel over stdin to a shared runner (`adapters/sandbox/runner.py`), never argv or env.
- The subprocess sandbox is defence in depth only (fake provider, tests): rlimits incl. `RLIMIT_NPROC=1` (no forks), empty read-only cwd, clean env, process-group kill after every run.
- Challenge code must also pass the AST allowlist in `domain/policy.py`.

## Round 2 hardening
- Policy bans introspection attributes, submodule access and dunder strings.
- Runner: minimal builtins (`policy.SAFE_BUILTINS`), restricted `__import__` returning module stand-ins without submodules, `sys` stand-in. A policy bypass alone no longer reaches `os`.
- `SubprocessSandbox` takes a required `source_allowed`; production passes `fake.is_template`, so model-written code can only ever run in Docker.
