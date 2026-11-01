"""``Uploader`` over ``gcloud storage cp``: file names go on stdin (``-I``), never argv, and symlinks are skipped."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from claude_duel.domain.errors import ConfigError

Runner = Callable[[Sequence[str], str], int]


def _gcloud(cmd: Sequence[str], stdin: str) -> int:
    return subprocess.run(list(cmd), input=stdin, text=True, check=False).returncode  # noqa: S603 - fixed argv


class GcloudUploader:
    def __init__(self, run: Runner = _gcloud) -> None:
        self._run = run

    def upload(self, files: Sequence[Path], dest: str) -> None:
        cmd = ["gcloud", "storage", "cp", "--ignore-symlinks", "-I", dest.rstrip("/") + "/"]
        code = self._run(cmd, "".join(f"{p}\n" for p in files))
        if code != 0:
            raise ConfigError(f"gcloud storage cp failed (exit {code})")
