"""R3-7: sandbox and app images are pinned by digest, and health() refuses an unpinned image."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claude_duel.adapters.sandbox import docker_sandbox
from claude_duel.adapters.sandbox.docker_sandbox import DEFAULT_IMAGE, DockerSandbox, unpinned
from claude_duel.config import Settings
from claude_duel.ports import RunResult

ROOT = Path(__file__).resolve().parents[2]
PIN = re.compile(r"python:3\.12-slim@sha256:[0-9a-f]{64}")


def test_every_image_reference_is_the_same_digest() -> None:
    assert unpinned(DEFAULT_IMAGE) is None
    assert Settings().docker_image == DEFAULT_IMAGE
    for name in ("Dockerfile", ".github/workflows/ci.yml"):
        text = (ROOT / name).read_text()
        assert DEFAULT_IMAGE in text
        assert "python:3.12-slim\n" not in text


@pytest.mark.parametrize("image", ["python:3.12-slim", "python@sha256:abc", "x@sha256:" + "G" * 64])
def test_unpinned_refs_are_refused(image: str) -> None:
    assert "not pinned by digest" in (unpinned(image) or "")


def test_health_refuses_an_unpinned_image(monkeypatch: pytest.MonkeyPatch) -> None:
    flag = docker_sandbox._SELF_TEST_FLAG
    monkeypatch.setattr(docker_sandbox, "docker_probe", lambda _docker: None)
    monkeypatch.setattr(DockerSandbox, "run", lambda *_a: RunResult(stdout=flag + "\n", exit_code=0, timed_out=False))
    assert DockerSandbox(image=DEFAULT_IMAGE).health() is None
    assert "not pinned" in (DockerSandbox(image="python:3.12-slim").health() or "")
    assert PIN.fullmatch(DEFAULT_IMAGE)
