"""Real Docker sandbox tests. Skipped when no Docker daemon is reachable (see the docker_cmd fixture)."""

from __future__ import annotations

import subprocess

import pytest

from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox
from claude_duel.app.container import make_sandbox
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import AgentSpec, SandboxLimits

pytestmark = pytest.mark.docker

FLAG = "DUEL{0123456789abcdef0123456789abcdef}"
LIM = SandboxLimits(wall_seconds=2, cpu_seconds=2, memory_mb=64, max_output_bytes=256, pids_limit=8)


@pytest.fixture(scope="module")
def sb(docker_cmd: tuple[str, ...]) -> DockerSandbox:
    return DockerSandbox(docker_cmd)


def _leftover(docker_cmd: tuple[str, ...], name_prefix: str = "duel-") -> str:
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


def test_flag_not_in_argv_or_environ(sb: DockerSandbox) -> None:
    assert not any(FLAG in a for a in sb.argv("duel-x", LIM))
    src = "import os\nprint(open('/proc/self/environ').read())\nprint(open('/proc/1/cmdline').read())"
    r = sb.run(src, "", FLAG, LIM.model_copy(update={"max_output_bytes": 4096}))
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


def test_no_network(sb: DockerSandbox) -> None:
    src = (
        "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 53), 2)\n    print('online')\n"
        "except OSError:\n    print('offline')"
    )
    assert sb.run(src, "", FLAG, LIM).stdout.strip() == "offline"


def test_read_only_non_root_no_host_files(sb: DockerSandbox) -> None:
    src = (
        "import os\nprint(os.getuid())\n"
        "try:\n    open('/x', 'w')\n    print('wrote')\nexcept OSError:\n    print('ro')\n"
        "print(os.path.exists('/workspace'), os.path.exists('/home/box'))"
    )
    assert sb.run(src, "", FLAG, LIM).stdout.split() == ["65534", "ro", "False", "False"]


def test_pids_and_memory_limits(sb: DockerSandbox) -> None:
    fork_bomb = (
        "import os, time\ntry:\n    while True:\n        if os.fork() == 0:\n            time.sleep(10)\n"
        "except OSError:\n    print('capped')"
    )
    assert "capped" in sb.run(fork_bomb, "", FLAG, LIM.model_copy(update={"wall_seconds": 5})).stdout
    r = sb.run("x = bytearray(512 * 1024 * 1024)\nprint('alive')", "", FLAG, LIM)
    assert "alive" not in r.stdout


def test_cpu_seconds_limit(sb: DockerSandbox) -> None:
    r = sb.run("while True:\n    pass\n", "", FLAG, LIM.model_copy(update={"cpu_seconds": 1, "wall_seconds": 10}))
    assert not r.timed_out
    assert r.killed_reason == "signal"
