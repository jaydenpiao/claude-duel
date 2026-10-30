"""``duel backup``: copy the replay files (event logs and secret-free state) to a GCS bucket.

Only ``<id>.events.jsonl`` and ``<id>.json`` are sent; ``<id>.secret`` never leaves the box.
The upload is one ``gcloud storage cp`` call (the operator's gcloud login); ``run`` is injectable for tests.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from claude_duel.domain.errors import ConfigError

Runner = Callable[[Sequence[str]], int]
_BUCKET = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9](/[\w./-]*)?$")


def _gcloud(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), check=False).returncode  # noqa: S603 - fixed argv, no shell


def replay_files(store_dir: Path) -> list[Path]:
    """Every replay file in the store, sorted; secrets are excluded by construction."""
    files = [*store_dir.glob("*.events.jsonl"), *store_dir.glob("*.json")]
    return sorted(p for p in files if p.is_file() and not p.name.endswith(".secret"))


def backup(store_dir: Path, bucket: str | None, run: Runner = _gcloud) -> list[Path]:
    if not bucket:
        raise ConfigError("no replay bucket: set DUEL_REPLAY_BUCKET or pass --bucket")
    if not _BUCKET.match(bucket):
        raise ConfigError(f"not a gs:// bucket: {bucket!r}")
    files = replay_files(store_dir)
    if not files:
        return []
    code = run(["gcloud", "storage", "cp", "--no-clobber", *map(str, files), bucket.rstrip("/") + "/"])
    if code != 0:
        raise ConfigError(f"gcloud storage cp failed (exit {code})")
    return files
