"""``duel backup``: copy finished matches' replay files (event log and secret-free state) to a GCS bucket.

Only ``<id>.events.jsonl`` and ``<id>.json`` of a match that is in a terminal stage and not locked are sent;
``<id>.secret`` never leaves the box. Symlinks are skipped (and gcloud gets ``--ignore-symlinks``), paths are
absolute and go to ``gcloud storage cp -I`` on stdin, so no file name is ever read as an option. Each file is
scanned for every flag variant of its match before upload, and a hit refuses the whole backup. Files are
overwritten on each run, so a log that grew since the last backup is re-sent. The bucket must stay private
with public access prevention enforced. The store and uploader come from ``container`` (review M19), and the flag
set is ``flags.match_flags``, the same one the engine's event guard uses (M20).
"""

from __future__ import annotations

import re
from pathlib import Path

from claude_duel.domain.errors import ConfigError, DuelError, FlagLeakError
from claude_duel.domain.flags import assert_no_flag, match_flags
from claude_duel.domain.models import MatchState
from claude_duel.domain.state_machine import TERMINAL
from claude_duel.ports import MatchStore, Uploader

_BUCKET = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9](/[\w./-]*)?$")


def _finished(store: MatchStore, match_id: str) -> MatchState | None:
    """The match with its secret if it is terminal and nobody holds its lock; else None (skipped this run)."""
    try:
        with store.lock(match_id):
            state = store.load(match_id)
    except DuelError:  # locked by a running match, or no secret
        return None
    return state if state.stage in TERMINAL else None


def replay_files(store_dir: Path, store: MatchStore) -> list[Path]:
    """Replay files of finished matches, absolute and sorted; secrets and symlinks are excluded.

    Raises ``FlagLeakError`` if any file holds a flag variant of its match."""
    root = store_dir.resolve()
    out: list[Path] = []
    for secret in sorted(root.glob("*.secret")):
        mid = secret.name.removesuffix(".secret")
        paths = (secret, root / f"{mid}.events.jsonl", root / f"{mid}.json")
        if any(p.is_symlink() for p in paths):
            continue  # a planted link could point anywhere (another secret, gcloud credentials)
        files = [p for p in paths[1:] if p.is_file()]
        if not files or (state := _finished(store, mid)) is None:
            continue
        flags = match_flags(state)
        for p in files:
            text = p.read_text(encoding="utf-8", errors="replace")
            for f in flags:
                try:
                    assert_no_flag(text, f, p.name)
                except FlagLeakError as exc:
                    raise FlagLeakError(f"{p.name}: flag variant found; not backed up") from exc
        out += files
    return sorted(out)


def backup(store_dir: Path, store: MatchStore, uploader: Uploader, bucket: str | None) -> list[Path]:
    if not bucket:
        raise ConfigError("no replay bucket: set DUEL_REPLAY_BUCKET or pass --bucket")
    if not _BUCKET.match(bucket):
        raise ConfigError(f"not a gs:// bucket: {bucket!r}")
    files = replay_files(store_dir, store) if store_dir.is_dir() else []
    if files:
        uploader.upload(files, bucket)
    return files
