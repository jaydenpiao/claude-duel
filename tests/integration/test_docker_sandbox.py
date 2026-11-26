"""Real Docker sandbox tests. Skipped when no Docker daemon is reachable (see the docker_cmd fixture)."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from claude_duel.adapters.sandbox.common import REAP_SECONDS, execute
from claude_duel.adapters.sandbox.docker_sandbox import (
    DENIED_SYSCALLS,
    SECCOMP_PROFILE,
    DockerSandbox,
    docker_runtimes,
)
from claude_duel.adapters.sandbox.runner import frame
from claude_duel.app.container import make_sandbox
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec, SandboxLimits
from claude_duel.ports import RunResult

_HOST_REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.docker

FLAG = "DUEL{0123456789abcdef0123456789abcdef}"
LIM = SandboxLimits(wall_seconds=2, cpu_seconds=2, memory_mb=64, max_output_bytes=256, pids_limit=64)


# Per-run prefix: leftover checks only see this run's containers, never a concurrent run's (round 2 N2).
PREFIX = f"duel-test-{uuid.uuid4().hex[:8]}-"


@pytest.fixture(scope="module")
def sb(docker_cmd: tuple[str, ...]) -> DockerSandbox:
    return DockerSandbox(docker_cmd, name_prefix=PREFIX)


def _leftover(docker_cmd: tuple[str, ...], name_prefix: str = PREFIX) -> str:
    out = subprocess.run(
        [*docker_cmd, "ps", "-aq", "--filter", f"name={name_prefix}"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_health_ok_and_broken_image(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    assert sb.health() is None
    broken = DockerSandbox(docker_cmd, image="claude-duel-missing-image:none")
    assert "self-test container failed" in (broken.health() or "")


def test_auto_rejects_broken_docker_for_real_models(docker_cmd: tuple[str, ...]) -> None:
    settings = Settings(sandbox="auto", docker_command=docker_cmd, docker_image="claude-duel-missing-image:none")
    with pytest.raises(ConfigError, match="self-test"):
        make_sandbox(settings, (AgentSpec(name="a", provider="openai"), AgentSpec(name="b")))


def test_flag_reveal(sb: DockerSandbox) -> None:
    r = sb.run("import sys\nif sys.stdin.read() == 'go':\n    print(FLAG)", "go", FLAG, LIM)
    assert r.stdout.strip() == FLAG
    assert sb.run("import sys\nif sys.stdin.read() == 'go':\n    print(FLAG)", "no", FLAG, LIM).stdout == ""


def _raw(
    sb: DockerSandbox, docker_cmd: tuple[str, ...], script: str, stdin: str = "", lim: SandboxLimits = LIM
) -> RunResult:
    """Run unrestricted Python in the exact sandbox container (same argv, runner swapped for script).

    The runner blocks os/open/socket, so container-boundary tests need this to probe the layer below it.
    """
    name = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    argv = sb.argv(name, lim)
    argv[-1] = script
    rm = [*docker_cmd, "rm", "-f", name]
    return execute(
        argv,
        stdin,
        lim,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        wall_seconds=lim.wall_seconds + 5,
        cleanup=lambda: subprocess.run(rm, capture_output=True, check=False),
    )


def test_flag_not_in_argv_or_environ(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    assert not any(FLAG in a for a in sb.argv("duel-x", LIM))
    probe = (
        "import json, sys\njson.loads(sys.stdin.readline())\n"
        "print(open('/proc/self/environ').read())\nprint(open('/proc/1/cmdline').read())"
    )
    r = _raw(sb, docker_cmd, probe, frame("pass", FLAG, ""), LIM.model_copy(update={"max_output_bytes": 4096}))
    assert r.exit_code == 0
    assert FLAG not in r.stdout


def test_timeout_removes_container(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    r = sb.run("while True:\n    pass\n", "", FLAG, LIM.model_copy(update={"cpu_seconds": 30}))
    assert r.timed_out
    assert r.killed_reason == "wall"
    assert _leftover(docker_cmd) == ""


def test_output_cap(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    r = sb.run("while True:\n    print('a' * 1000)", "", FLAG, LIM)
    assert r.killed_reason == "output"
    assert len(r.stdout) == LIM.max_output_bytes
    assert _leftover(docker_cmd) == ""


def test_no_network(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    src = (
        "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 53), 2)\n    print('online')\n"
        "except OSError:\n    print('offline')"
    )
    assert _raw(sb, docker_cmd, src).stdout.strip() == "offline"


def test_read_only_non_root_no_host_files(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    src = (
        "import os\nprint(os.getuid())\n"
        "try:\n    open('/x', 'w')\n    print('wrote')\nexcept OSError:\n    print('ro')\n"
        f"print(os.path.exists({str(_HOST_REPO)!r}), os.path.exists({str(Path.home())!r}))"
    )
    assert _raw(sb, docker_cmd, src).stdout.split() == ["65534", "ro", "False", "False"]


def test_pids_and_memory_limits(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    # runc fails fork() with EAGAIN at the cap; gVisor (forced on box CI) kills the sandbox. Either way the
    # bomb never gets past 64 live pids, while a modest 16-child fan-out still works.
    bomb = (
        "import os, time\ntry:\n    for _ in range(10000):\n        if os.fork() == 0:\n            time.sleep(10)\n"
        "            os._exit(0)\n    print('escaped')\nexcept OSError:\n    print('capped')"
    )
    lim5 = LIM.model_copy(update={"wall_seconds": 5})
    assert "escaped" not in _raw(sb, docker_cmd, bomb, lim=lim5).stdout
    fan = "import os\nfor _ in range(16):\n    if os.fork() == 0:\n        os._exit(0)\n    os.wait()\nprint('ok')"
    assert _raw(sb, docker_cmd, fan, lim=lim5).stdout.strip() == "ok"
    r = sb.run("x = bytearray(512 * 1024 * 1024)\nprint('alive')", "", FLAG, LIM)
    assert "alive" not in r.stdout


def test_runner_blocks_os_in_container(sb: DockerSandbox) -> None:  # round 2 N1, inside the real boundary
    r = sb.run("def g():\n    yield 1\ng().gi_frame.f_builtins['__import__']('os')", "", FLAG, LIM)
    assert r.exit_code != 0


def test_cpu_seconds_limit(sb: DockerSandbox) -> None:
    # Generous wall limit: on a loaded host, 1 CPU-second under --cpus can take well over 10s of wall time.
    r = sb.run("while True:\n    pass\n", "", FLAG, LIM.model_copy(update={"cpu_seconds": 1, "wall_seconds": 60}))
    assert not r.timed_out
    assert r.killed_reason == "signal"


def test_runsc_runtime_runs_hardened(docker_cmd: tuple[str, ...]) -> None:
    if "runsc" not in docker_runtimes(docker_cmd):
        pytest.skip("gVisor runsc runtime not registered")
    gv = DockerSandbox(docker_cmd, name_prefix=PREFIX, runtime="runsc")
    lim = LIM
    assert gv.health() is None
    r = gv.run("import sys\nif sys.stdin.read() == 'go':\n    print(FLAG)", "go", FLAG, lim)
    assert r.stdout.strip() == FLAG
    assert gv.run("while True: pass", "", FLAG, lim).killed_reason is not None
    bomb = "import os\nwhile True:\n    os.fork()"
    assert FLAG not in gv.run(bomb, "", FLAG, lim).stdout
    assert _leftover(docker_cmd) == ""


# A same-uid ptrace of the in-container `timeout` watchdog stops it (state T), so its wall kill never fires.
PTRACE = r"""
import ctypes, os
print("attach", ctypes.CDLL(None).ptrace(16, os.getppid(), 0, 0), flush=True)
os._exit(0)
"""
HOST_DEADLINE = LIM.wall_seconds + 5  # _raw's host wall
REAP_MARGIN = 2 * REAP_SECONDS + 10  # bounded reaps plus `docker rm -f`


def test_seccomp_denies_ptrace_and_returns_promptly(sb: DockerSandbox, docker_cmd: tuple[str, ...]) -> None:
    started = time.monotonic()
    r = _raw(sb, docker_cmd, PTRACE)
    assert time.monotonic() - started < HOST_DEADLINE + REAP_MARGIN
    assert r.stdout.split() == ["attach", "-1"]  # EPERM from the custom profile
    assert _leftover(docker_cmd) == ""


def test_host_deadline_removes_a_container_whose_watchdog_is_stopped(
    sb: DockerSandbox, docker_cmd: tuple[str, ...]
) -> None:
    """Without the profile, the ptrace stops `timeout`; the host kill deadline must still end the run and remove
    the container, also when the docker client is privileged (sudo) and survives the process-group kill."""
    name = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    argv = sb.argv(name, LIM)
    at = argv.index(f"seccomp={SECCOMP_PROFILE}")
    del argv[at - 1 : at + 1]  # Docker's default profile: ptrace allowed
    argv[-1] = PTRACE
    rm = [*docker_cmd, "rm", "-f", name]
    started = time.monotonic()
    r = execute(
        argv,
        "",
        LIM,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        wall_seconds=HOST_DEADLINE,
        cleanup=lambda: subprocess.run(rm, capture_output=True, check=False),
    )
    assert time.monotonic() - started < HOST_DEADLINE + REAP_MARGIN
    # gVisor (runsc, forced on CI) denies the attach itself, so nothing freezes and the run ends normally.
    assert r.timed_out or r.stdout.split() == ["attach", "-1"]
    assert _leftover(docker_cmd) == ""


def test_profile_is_dockers_default_minus_the_tracing_calls() -> None:
    profile = json.loads(SECCOMP_PROFILE.read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {n for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" for n in rule["names"]}
    assert not allowed & DENIED_SYSCALLS
    assert {"read", "write", "execve", "clone", "mmap"} <= allowed
