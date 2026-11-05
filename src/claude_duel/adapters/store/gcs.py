"""``Uploader`` over ``gcloud storage cp``: file names go on stdin (``-I``), never argv, and symlinks are skipped.

Nothing is sent unless the destination bucket has public access prevention enforced (``assert_private``).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from claude_duel.domain.errors import ConfigError

Runner = Callable[[Sequence[str], str], int]
Describer = Callable[[str], str]


def _gcloud(cmd: Sequence[str], stdin: str) -> int:
    return subprocess.run(list(cmd), input=stdin, text=True, check=False).returncode  # noqa: S603 - fixed argv


def _describe(bucket: str) -> str:
    cmd = ["gcloud", "storage", "buckets", "describe", bucket, "--format=json"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout  # noqa: S603 - fixed argv


def assert_private(bucket: str, describe: Describer) -> None:
    """The bucket must have public access prevention enforced, or nothing is sent."""
    root = "gs://" + bucket.removeprefix("gs://").split("/", 1)[0]
    try:
        info = json.loads(describe(root) or "{}")
    except ValueError:
        info = {}
    if not isinstance(info, dict) or info.get("public_access_prevention") != "enforced":
        raise ConfigError(f"{root}: public access prevention is not enforced; refusing to back up")


class GcloudUploader:
    def __init__(self, run: Runner = _gcloud, describe: Describer = _describe) -> None:
        self._run = run
        self._describe = describe

    def upload(self, files: Sequence[Path], dest: str) -> None:
        assert_private(dest, self._describe)
        cmd = ["gcloud", "storage", "cp", "--ignore-symlinks", "-I", dest.rstrip("/") + "/"]
        code = self._run(cmd, "".join(f"{p}\n" for p in files))
        if code != 0:
            raise ConfigError(f"gcloud storage cp failed (exit {code})")
