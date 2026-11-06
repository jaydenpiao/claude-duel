"""``duel backup`` sends finished matches' replay files and never a secret, symlink or flag (fake runner)."""

from __future__ import annotations

import base64
import os
import zlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from claude_duel.adapters.store.file_store import FileLock, FileMatchStore
from claude_duel.adapters.store.gcs import GcloudUploader
from claude_duel.app import backup as backup_mod
from claude_duel.app.engine import MatchEngine
from claude_duel.domain.errors import ConfigError, FlagLeakError
from claude_duel.domain.flags import HEX_PATTERN, KEY_PATTERNS, SECRET_PATTERNS, flag_of, match_flags
from claude_duel.domain.models import AgentSpec, Challenge, Event, MatchState, Rules, SandboxLimits, Stage

BUCKET = "gs://project-0fedfc79-0519-43dd-8a0-duel-replays"
PRIVATE = '{"public_access_prevention": "enforced"}'


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


def backup(root: Path, bucket: str | None, run: Gcloud) -> list[Path]:
    return backup_mod.backup(FileMatchStore(root), GcloudUploader(run, lambda _: PRIVATE), bucket).files


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


def _rekeyed() -> MatchState:
    """c1 was built on its third try, though only one repair error was kept (re-keyed epoch)."""
    return MatchState(
        match_id="m",
        secret="s3cret",
        agents=(AgentSpec(name="alice"), AgentSpec(name="bob")),
        rules=Rules(rounds=1),
        limits=SandboxLimits(),
        challenges={"c1": Challenge(id="c1", round=1, author="alice", spec=None, flag_epoch=2)},
        build_errors={"c1": ["x"], "c2": ["y", "z"]},
    )


def test_match_flags_cover_every_epoch_and_extra_ids() -> None:
    flags = match_flags(_rekeyed(), ["c3"])
    want = [flag_of("s3cret", "c1", e) for e in range(3)] + [flag_of("s3cret", "c2", e) for e in range(3)]
    assert flags == [*want, flag_of("s3cret", "c3", 0)]
    assert match_flags(_rekeyed().model_copy(update={"secret": ""})) == []


def test_backup_and_engine_share_one_flag_set(tmp_path: Path) -> None:
    """M20: the built epoch's flag refuses the backup and the engine's event guard alike."""
    state = _rekeyed().model_copy(update={"stage": Stage.COMPLETE})
    FileMatchStore(tmp_path).save(state)
    leak = flag_of("s3cret", "c1", 2)
    (tmp_path / "m.events.jsonl").write_text(leak + "\n")
    with pytest.raises(FlagLeakError):
        backup(tmp_path, BUCKET, Gcloud())
    with pytest.raises(FlagLeakError):
        MatchEngine._no_flags(state, Event(seq=1, trace_id="t", type="note", data={"text": leak}))


@pytest.mark.parametrize(
    "leak",
    ["sk-proj-" + "A" * 24, "sk-ant-api03-" + "B" * 24, "api_key=" + "c" * 12],
    ids=["openai", "anthropic", "api-key-assignment"],
)
def test_an_api_key_shape_skips_only_that_match(tmp_path: Path, leak: str) -> None:
    """M24: a key shape (often just model text) holds back its match, never uploaded, and the run goes on."""
    _match(tmp_path, "m1", log=f'{{"note": "{leak}"}}\n')
    _match(tmp_path, "m2")
    run = Gcloud()
    result = backup_mod.backup(FileMatchStore(tmp_path), GcloudUploader(run, lambda _: PRIVATE), BUCKET)
    assert [p.name for p in result.files] == ["m2.events.jsonl", "m2.json"]
    assert result.skipped == ["m1.events.jsonl: API-key-shaped text; match m1 not backed up"]
    ((_, paths),) = run.calls
    assert not any("m1." in p for p in paths)


def test_key_scan_is_the_named_key_patterns() -> None:
    """M24: the backup scans by name, not by position in SECRET_PATTERNS; the long-hex rule (event hashes) is out."""
    assert backup_mod.KEY_PATTERNS is KEY_PATTERNS
    assert HEX_PATTERN not in KEY_PATTERNS
    assert (*KEY_PATTERNS, HEX_PATTERN) == SECRET_PATTERNS
    assert not any(k.search("a" * 64) for k in KEY_PATTERNS)


class _MemSource:
    """A non-file store: backup lists files only through ``ReplaySource.replays`` (M23)."""

    def __init__(self, root: Path, state: MatchState) -> None:
        self.state, self.files = state, [root / "elsewhere.events.jsonl"]
        self.files[0].write_text("{}\n")

    def replays(self) -> list[tuple[str, list[Path]]]:
        return [(self.state.match_id, self.files)]

    def lock(self, match_id: str) -> FileLock:
        return FileLock(self.files[0].with_suffix(".lock"))

    def load(self, match_id: str) -> MatchState:
        return self.state

    def save(self, state: MatchState) -> None: ...

    def snapshot(self, match_id: str) -> MatchState:
        return self.state


def test_backup_lists_files_through_the_store_port(tmp_path: Path) -> None:
    state = _rekeyed().model_copy(update={"stage": Stage.COMPLETE})
    (tmp_path / "m9.events.jsonl").write_text("{}\n")  # a stray file in the dir: never globbed
    run = Gcloud()
    result = backup_mod.backup(_MemSource(tmp_path, state), GcloudUploader(run, lambda _: PRIVATE), BUCKET)
    assert [p.name for p in result.files] == ["elsewhere.events.jsonl"]
    assert run.calls[0][1] == [str(tmp_path / "elsewhere.events.jsonl")]


def test_the_match_secret_refuses_the_backup(tmp_path: Path) -> None:
    _match(tmp_path, "m1", log='{"note": "s3cret"}\n')
    run = Gcloud()
    with pytest.raises(FlagLeakError, match="secret"):
        backup(tmp_path, BUCKET, run)
    assert run.calls == []


def test_odd_match_ids_are_never_listed(tmp_path: Path) -> None:
    _match(tmp_path, "m1")
    for ext in ("secret", "json", "events.jsonl"):  # the store would refuse this id, so plant the files by hand
        (tmp_path / f"m_2.{ext}").write_bytes((tmp_path / f"m1.{ext}").read_bytes())
    sent = backup(tmp_path, BUCKET, Gcloud())
    assert {p.name for p in sent} == {"m1.events.jsonl", "m1.json"}


@pytest.mark.parametrize("info", ['{"public_access_prevention": "inherited"}', "{}", "[]", "not json", ""])
def test_bucket_without_public_access_prevention_is_refused(tmp_path: Path, info: str) -> None:
    _match(tmp_path, "m1")
    run = Gcloud()
    with pytest.raises(ConfigError, match="public access prevention"):
        backup_mod.backup(FileMatchStore(tmp_path), GcloudUploader(run, lambda _: info), BUCKET + "/x")
    assert run.calls == []


def test_private_check_uses_the_bucket_root(tmp_path: Path) -> None:
    _match(tmp_path, "m1")
    seen: list[str] = []
    uploader = GcloudUploader(Gcloud(), lambda b: seen.append(b) or PRIVATE)
    backup_mod.backup(FileMatchStore(tmp_path), uploader, BUCKET + "/a/b")
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
def test_rare_encodings_refuse_the_backup(tmp_path: Path, index: int) -> None:
    flag = flag_of("s3cret", "c1", 0)
    _match(tmp_path, "m1", log=f'{{"text": "{_forms(flag)[index]}"}}\n')
    with pytest.raises(FlagLeakError):
        backup(tmp_path, BUCKET, Gcloud())


def test_ordinary_log_passes_the_deep_scan() -> None:
    backup_mod.scan_deep(
        '{"payload": "1000 337", "b64": "aGVsbG8gd29ybGQgdGhpcyBpcyBmaW5l", "n": 1010101}',
        "DUEL{" + "ab" * 16 + "}",
        "x",
    )
