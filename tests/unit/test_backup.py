"""``duel backup`` sends replay files to the GCS bucket and never a secret (fake runner, no cloud calls)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from claude_duel.app.backup import backup
from claude_duel.domain.errors import ConfigError

BUCKET = "gs://project-0fedfc79-0519-43dd-8a0-duel-replays"


def test_backup_uploads_logs_and_state_but_never_secrets(tmp_path: Path) -> None:
    for name in ("m1.events.jsonl", "m1.json", "m1.secret", "notes.txt"):
        (tmp_path / name).write_text("x")
    calls: list[list[str]] = []

    def run(cmd: Sequence[str]) -> int:
        calls.append(list(cmd))
        return 0

    sent = backup(tmp_path, BUCKET, run)
    assert [p.name for p in sent] == ["m1.events.jsonl", "m1.json"]
    (cmd,) = calls
    assert cmd[:4] == ["gcloud", "storage", "cp", "--no-clobber"]
    assert cmd[-1] == BUCKET + "/"
    assert not any("secret" in c for c in cmd)


def test_backup_refuses_a_bad_bucket_or_a_failed_copy(tmp_path: Path) -> None:
    (tmp_path / "m1.json").write_text("{}")
    for bad in (None, "", "s3://x", "gs://UP"):
        with pytest.raises(ConfigError):
            backup(tmp_path, bad, lambda _: 0)
    with pytest.raises(ConfigError, match="exit 1"):
        backup(tmp_path, BUCKET, lambda _: 1)
    assert backup(tmp_path / "empty", BUCKET, lambda _: 1) == []
