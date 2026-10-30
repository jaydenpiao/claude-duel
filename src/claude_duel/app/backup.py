"""``duel backup``: copy finished matches' replay files (event log and secret-free state) to a GCS bucket.

Only ``<id>.events.jsonl`` and ``<id>.json`` of a match that is in a terminal stage and not locked are sent;
``<id>.secret`` never leaves the box. Symlinks are skipped (and gcloud gets ``--ignore-symlinks``), paths are
absolute and go to ``gcloud storage cp -I`` on stdin, so no file name is ever read as an option. Each file is
scanned for every flag variant of its match before upload, and a hit refuses the whole backup. Files are
overwritten on each run, so a log that grew since the last backup is re-sent. The bucket must stay private
with public access prevention enforced. ``run`` is injectable for tests.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.domain.errors import ConfigError, DuelError, FlagLeakError
from claude_duel.domain.flags import assert_no_flag, flag_of
from claude_duel.domain.models import MatchState
from claude_duel.domain.state_machine import TERMINAL

Runner = Callable[[Sequence[str], str], int]
_BUCKET = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9](/[\w./-]*)?$")


def _gcloud(cmd: Sequence[str], stdin: str) -> int:
    return subprocess.run(list(cmd), input=stdin, text=True, check=False).returncode  # noqa: S603 - fixed argv


def _flags(state: MatchState) -> list[str]:
    """Every flag the match ever used: each challenge, each repair try (as in the engine's event guard)."""
    cids = set(state.challenges) | set(state.build_errors)
    return [
        flag_of(state.secret, cid, epoch)
        for cid in sorted(cids)
        for epoch in range(
            max(len(state.build_errors.get(cid, [])), getattr(state.challenges.get(cid), "flag_epoch", 0)) + 1
        )
    ]


def _finished(store: FileMatchStore, match_id: str) -> MatchState | None:
    """The match with its secret if it is terminal and nobody holds its lock; else None (skipped this run)."""
    try:
        with store.lock(match_id):
            state = store.load(match_id)
    except DuelError:  # locked by a running match, or no secret
        return None
    return state if state.stage in TERMINAL else None


def replay_files(store_dir: Path) -> list[Path]:
    """Replay files of finished matches, absolute and sorted; secrets and symlinks are excluded.

    Raises ``FlagLeakError`` if any file holds a flag variant of its match."""
    root = store_dir.resolve()
    store = FileMatchStore(root)
    out: list[Path] = []
    for secret in sorted(root.glob("*.secret")):
        mid = secret.name.removesuffix(".secret")
        paths = (secret, root / f"{mid}.events.jsonl", root / f"{mid}.json")
        if any(p.is_symlink() for p in paths):
            continue  # a planted link could point anywhere (another secret, gcloud credentials)
        files = [p for p in paths[1:] if p.is_file()]
        if not files or (state := _finished(store, mid)) is None:
            continue
        flags = _flags(state)
        for p in files:
            text = p.read_text(encoding="utf-8", errors="replace")
            for f in flags:
                try:
                    assert_no_flag(text, f, p.name)
                except FlagLeakError as exc:
                    raise FlagLeakError(f"{p.name}: flag variant found; not backed up") from exc
        out += files
    return sorted(out)


def backup(store_dir: Path, bucket: str | None, run: Runner = _gcloud) -> list[Path]:
    if not bucket:
        raise ConfigError("no replay bucket: set DUEL_REPLAY_BUCKET or pass --bucket")
    if not _BUCKET.match(bucket):
        raise ConfigError(f"not a gs:// bucket: {bucket!r}")
    files = replay_files(store_dir) if store_dir.is_dir() else []
    if not files:
        return []
    cmd = ["gcloud", "storage", "cp", "--ignore-symlinks", "-I", bucket.rstrip("/") + "/"]
    code = run(cmd, "".join(f"{p}\n" for p in files))
    if code != 0:
        raise ConfigError(f"gcloud storage cp failed (exit {code})")
    return files
