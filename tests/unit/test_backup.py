"""``duel backup`` sends finished matches' replay files and never a secret, symlink or flag (fake runner)."""

from __future__ import annotations

import base64
import os
import zlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.app import backup as backup_mod
from claude_duel.app.backup import backup, scan_deep
from claude_duel.domain.errors import ConfigError, FlagLeakError
from claude_duel.domain.flags import flag_of
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits, Stage

BUCKET = "gs://project-0fedfc79-0519-43dd-8a0-duel-replays"


def _match(root: Path, mid: str, stage: Stage = Stage.COMPLETE, log: str = "{}\n") -> None:
    state = MatchState(
        match_id=mid,
        secret="s3cret",
        agents=(AgentSpec(name="alice"), AgentSpec(name="bob")),
        rules=Rules(rounds=1),
        limits=SandboxLimits(),
        stage=stage,
        build_errors={"c1": []},
    )
    FileMatchStore(root).save(state)
    (root / f"{mid}.events.jsonl").write_text(log)


class Gcloud:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[list[str], list[str]]] = []

    def __call__(self, cmd: Sequence[str], stdin: str) -> int:
        self.calls.append((list(cmd), stdin.splitlines()))
        return self.code


def test_backup_sends_finished_matches_but_never_secrets(tmp_path: Path) -> None:
    _match(tmp_path, "m1")
    _match(tmp_path, "m2", Stage.ATTACK)  # still running: skipped
    (tmp_path / "notes.txt").write_text("x")
    run = Gcloud()
    sent = backup(tmp_path, BUCKET, run)
    assert [p.name for p in sent] == ["m1.events.jsonl", "m1.json"]
    ((cmd, paths),) = run.calls
    assert cmd == ["gcloud", "storage", "cp", "--ignore-symlinks", "-I", BUCKET + "/"]
    assert all(Path(p).is_absolute() and not p.endswith(".secret") for p in paths)


def test_locked_match_is_skipped(tmp_path: Path) -> None:
    _match(tmp_path, "m1")
    with FileMatchStore(tmp_path).lock("m1"):
        assert backup(tmp_path, BUCKET, Gcloud()) == []


def test_symlinks_are_skipped_and_dash_names_stay_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _match(tmp_path, "-x")
    _match(tmp_path, "m1")
    (tmp_path / "m1.json").unlink()
    (tmp_path / "m1.json").symlink_to(tmp_path / "m1.secret")
    monkeypatch.chdir(tmp_path)
    run = Gcloud()
    sent = backup(Path(), BUCKET, run)
    assert [p.name for p in sent] == ["-x.events.jsonl", "-x.json"]  # m1 has a symlink: skipped
    ((cmd, paths),) = run.calls
    assert not any(c.startswith("-x") for c in cmd)  # names only on stdin, never argv
    assert paths[0] == str(tmp_path.resolve() / "-x.events.jsonl")


def test_a_flag_variant_refuses_the_backup(tmp_path: Path) -> None:
    flag = flag_of("s3cret", "c1")
    _match(tmp_path, "m1", log=flag[::-1] + "\n")
    run = Gcloud()
    with pytest.raises(FlagLeakError, match=r"m1\.events\.jsonl"):
        backup(tmp_path, BUCKET, run)
    assert run.calls == []


def test_a_file_changed_after_backup_is_sent_again(tmp_path: Path) -> None:
    _match(tmp_path, "m1")
    run = Gcloud()
    backup(tmp_path, BUCKET, run)
    with (tmp_path / "m1.events.jsonl").open("a") as f:
        f.write("{}\n")
    backup(tmp_path, BUCKET, run)
    assert str((tmp_path / "m1.events.jsonl").resolve()) in run.calls[1][1]
    assert "--no-clobber" not in run.calls[1][0]


def test_backup_refuses_a_bad_bucket_or_a_failed_copy(tmp_path: Path) -> None:
    _match(tmp_path, "m1")
    for bad in (None, "", "s3://x", "gs://UP"):
        with pytest.raises(ConfigError):
            backup(tmp_path, bad, Gcloud())
    with pytest.raises(ConfigError, match="exit 1"):
        backup(tmp_path, BUCKET, Gcloud(1))
    assert backup(tmp_path / "empty", BUCKET, Gcloud(1)) == []
    assert os.access(tmp_path / "m1.secret", os.R_OK)


@pytest.fixture(autouse=True)
def _bucket_is_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_mod, "_describe", lambda _: '{"public_access_prevention": "enforced"}')


@pytest.mark.parametrize("info", ['{"public_access_prevention": "inherited"}', "{}", "not json", ""])
def test_bucket_without_public_access_prevention_is_refused(tmp_path: Path, info: str) -> None:
    _match(tmp_path, "m1")
    run = Gcloud()
    with pytest.raises(ConfigError, match="public access prevention"):
        backup(tmp_path, BUCKET + "/x", run, describe=lambda _: info)
    assert run.calls == []


def test_private_check_uses_the_bucket_root(tmp_path: Path) -> None:
    _match(tmp_path, "m1")
    seen: list[str] = []
    backup(
        tmp_path,
        BUCKET + "/a/b",
        Gcloud(),
        describe=lambda b: seen.append(b) or '{"public_access_prevention": "enforced"}',
    )
    assert seen == [BUCKET]


def _forms(flag: str) -> list[str]:
    raw = flag.encode()
    return [
        " ".join(f"{b:o}" for b in raw),
        " ".join(f"{b:08b}" for b in raw),
        base64.b85encode(raw).decode(),
        base64.b64encode(zlib.compress(raw)).decode(),
        base64.b64encode(base64.b64encode(raw)).decode(),
    ]


@pytest.mark.parametrize("index", range(5))
def test_s5_3_encodings_refuse_the_backup(tmp_path: Path, index: int) -> None:
    flag = flag_of("s3cret", "c1", 0)
    _match(tmp_path, "m1", log=f'{{"text": "{_forms(flag)[index]}"}}\n')
    with pytest.raises(FlagLeakError):
        backup(tmp_path, BUCKET, Gcloud())


def test_ordinary_log_passes_the_deep_scan() -> None:
    scan_deep(
        '{"payload": "1000 337", "b64": "aGVsbG8gd29ybGQgdGhpcyBpcyBmaW5l", "n": 1010101}',
        "DUEL{" + "ab" * 16 + "}",
        "x",
    )
