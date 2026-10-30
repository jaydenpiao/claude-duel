"""``duel backup``: copy finished matches' replay files (event log and secret-free state) to a GCS bucket.

Only ``<id>.events.jsonl`` and ``<id>.json`` of a match that is in a terminal stage and not locked are sent;
``<id>.secret`` never leaves the box. Symlinks are skipped (and gcloud gets ``--ignore-symlinks``), paths are
absolute and go to ``gcloud storage cp -I`` on stdin, so no file name is ever read as an option. Each file is
scanned for every flag variant of its match before upload, and a hit refuses the whole backup. Files are
overwritten on each run, so a log that grew since the last backup is re-sent. The bucket must stay private
with public access prevention enforced: ``backup`` checks that with ``gcloud storage buckets describe`` and refuses
otherwise. Besides ``assert_no_flag``, files are scanned for rarer encodings (octal, binary, base85, and base64 or
zlib+base64 blobs decoded and rescanned). ``run`` and ``describe`` are injectable for tests.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import re
import subprocess
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path

from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.domain.errors import ConfigError, DuelError, FlagLeakError
from claude_duel.domain.flags import assert_no_flag, flag_of
from claude_duel.domain.models import MatchState
from claude_duel.domain.state_machine import TERMINAL

Runner = Callable[[Sequence[str], str], int]
Describer = Callable[[str], str]
_B64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_B85 = re.compile(r"[0-9A-Za-z!#$%&()*+\-;<=>?@^_`{|}~]{20,}")
_BUCKET = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9](/[\w./-]*)?$")


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
    if info.get("public_access_prevention") != "enforced":
        raise ConfigError(f"{root}: public access prevention is not enforced; refusing to back up")


def _codes(flag: str) -> list[str]:
    """The flag as octal and binary char codes, separators removed."""
    return ["".join(f"{ord(c):o}" for c in flag), "".join(f"{ord(c):08b}" for c in flag)]


def _decoded(text: str) -> list[str]:
    """Base64 (optionally zlib-compressed) and base85 blobs in text, decoded."""
    out: list[str] = []
    for blob in _B64.findall(text):
        try:
            raw = base64.urlsafe_b64decode(blob.replace("+", "-").replace("/", "_") + "==")
        except (binascii.Error, ValueError):
            continue
        out.append(raw.decode("latin-1"))
        with contextlib.suppress(zlib.error):
            out.append(zlib.decompressobj().decompress(raw, 1 << 16).decode("latin-1"))  # capped: no zip bombs
    for blob in _B85.findall(text):
        for dec in (base64.b85decode, base64.a85decode):
            try:
                out.append(dec(blob).decode("latin-1"))
            except ValueError:
                continue
    return out


def scan_deep(text: str, flag: str, where: str) -> None:
    """``assert_no_flag`` plus the rarer encodings it misses (octal, binary, base85, base64 and zlib blobs)."""
    assert_no_flag(text, flag, where)
    digits = (re.sub(r"[^0-7]", "", text), re.sub(r"[^01]", "", text))
    if any(code in d for code, d in zip(_codes(flag), digits, strict=True)):
        raise FlagLeakError(f"flag would leak via {where} (char codes)")
    for plain in _decoded(text):
        assert_no_flag(plain, flag, f"{where} (decoded)")


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
                    scan_deep(text, f, p.name)
                except FlagLeakError as exc:
                    raise FlagLeakError(f"{p.name}: flag variant found; not backed up") from exc
        out += files
    return sorted(out)


def backup(store_dir: Path, bucket: str | None, run: Runner = _gcloud, describe: Describer | None = None) -> list[Path]:
    if not bucket:
        raise ConfigError("no replay bucket: set DUEL_REPLAY_BUCKET or pass --bucket")
    if not _BUCKET.match(bucket):
        raise ConfigError(f"not a gs:// bucket: {bucket!r}")
    files = replay_files(store_dir) if store_dir.is_dir() else []
    if not files:
        return []
    assert_private(bucket, describe or _describe)
    cmd = ["gcloud", "storage", "cp", "--ignore-symlinks", "-I", bucket.rstrip("/") + "/"]
    code = run(cmd, "".join(f"{p}\n" for p in files))
    if code != 0:
        raise ConfigError(f"gcloud storage cp failed (exit {code})")
    return files
