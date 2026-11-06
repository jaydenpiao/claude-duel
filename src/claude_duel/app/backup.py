"""``duel backup``: copy finished matches' replay files (event log and secret-free state) to a GCS bucket.

Only ``<id>.events.jsonl`` and ``<id>.json`` of a match that is in a terminal stage and not locked are sent;
``<id>.secret`` never leaves the box. The file list comes from the store (``ReplaySource.replays``, which skips
symlinked matches), and gcloud gets ``--ignore-symlinks``; paths are absolute and go to ``gcloud storage cp -I`` on
stdin, so no file name is ever read as an option. Each file is scanned before upload: a flag variant of its match
or the match secret refuses the whole backup; an API-key shape (``flags.KEY_PATTERNS``, which ordinary model text
can trip) skips only that match, which is reported and never sent. Files are overwritten on each run, so a log that
grew since the last backup is re-sent. Besides ``assert_no_flag``, files are scanned for rarer encodings (octal,
binary, base85, and base64 or zlib+base64 blobs decoded and rescanned, ``scan_deep``). The bucket must stay private
with public access prevention enforced; the ``Uploader`` checks that before it sends anything. The
store and uploader come from ``container`` (review M19, M23), and the flag set is ``flags.match_flags``, the same one
the engine's event guard uses (M20).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from claude_duel.domain.errors import ConfigError, DuelError, FlagLeakError
from claude_duel.domain.flags import KEY_PATTERNS, assert_no_flag, match_flags
from claude_duel.domain.models import MatchState
from claude_duel.domain.state_machine import TERMINAL
from claude_duel.ports import MatchStore, ReplaySource, Uploader

_B64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_B85 = re.compile(r"[0-9A-Za-z!#$%&()*+\-;<=>?@^_`{|}~]{20,}")
_BUCKET = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9](/[\w./-]*)?$")


@dataclass
class Backup:
    files: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "<file>: why" for matches held back by the key scan


def _finished(store: MatchStore, match_id: str) -> MatchState | None:
    """The match with its secret if it is terminal and nobody holds its lock; else None (skipped this run)."""
    try:
        with store.lock(match_id):
            state = store.load(match_id)
    except DuelError:  # locked by a running match, or no secret
        return None
    return state if state.stage in TERMINAL else None


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


def _has_key(path: Path, state: MatchState) -> bool:
    """Raise on a flag variant or the match secret (S10-2); return whether an API-key shape is present."""
    text = path.read_text(encoding="utf-8", errors="replace")
    for f in match_flags(state):
        try:
            scan_deep(text, f, path.name)
        except FlagLeakError as exc:
            raise FlagLeakError(f"{path.name}: flag variant found; not backed up") from exc
    if state.secret and state.secret in text:
        raise FlagLeakError(f"{path.name}: the match secret found; not backed up")
    return any(k.search(text) for k in KEY_PATTERNS)


def replay_files(source: ReplaySource) -> Backup:
    """Replay files of finished matches, sorted; matches with an API-key shape are skipped and listed.

    Raises ``FlagLeakError`` if any file holds a flag variant of its match or the match secret."""
    out = Backup()
    for mid, files in source.replays():
        if (state := _finished(source, mid)) is None:
            continue
        keyed = [p.name for p in files if _has_key(p, state)]
        if keyed:
            out.skipped += [f"{n}: API-key-shaped text; match {mid} not backed up" for n in keyed]
        else:
            out.files += files
    out.files.sort()
    return out


def backup(source: ReplaySource, uploader: Uploader, bucket: str | None) -> Backup:
    if not bucket:
        raise ConfigError("no replay bucket: set DUEL_REPLAY_BUCKET or pass --bucket")
    if not _BUCKET.match(bucket):
        raise ConfigError(f"not a gs:// bucket: {bucket!r}")
    result = replay_files(source)
    if result.files:
        uploader.upload(result.files, bucket)
    return result
