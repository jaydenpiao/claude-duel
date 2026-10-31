"""The container runs python under ``env -i``: the runner flags rely on an empty env, not ``-E``."""

from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox
from claude_duel.domain.models import SandboxLimits


def test_env_i_before_python() -> None:
    argv = DockerSandbox(("docker",), image="img@sha256:" + "0" * 64).argv("duel-x", SandboxLimits())
    image = argv.index("img@sha256:" + "0" * 64)
    cmd = argv[image + 1 :]
    assert cmd[:2] == ["env", "-i"]
    assert cmd.index("python") > 1
    env_vars = cmd[2 : cmd.index("timeout")]
    assert all("=" in v for v in env_vars)
    assert {v.split("=")[0] for v in env_vars} <= {"PATH", "PYTHONHASHSEED"}
