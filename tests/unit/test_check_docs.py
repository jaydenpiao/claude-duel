"""scripts/check_docs.py: passes on this repo and catches each kind of drift."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from claude_duel import cli, config

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_docs", ROOT / "scripts" / "check_docs.py")
assert _spec is not None
assert _spec.loader is not None
check_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docs)


def test_repo_docs_are_in_sync() -> None:
    assert check_docs.main() == 0


def _repo(tmp: Path, agents: str, readme: str = "") -> Path:
    pkg = tmp / "src" / "claude_duel" / "app"
    pkg.mkdir(parents=True)
    (pkg / "engine.py").write_text("")
    (tmp / "src" / "claude_duel" / "ports.py").write_text(
        "from typing import Protocol\nclass Sandbox(Protocol): ...\nclass Other: ...\n"
    )
    (tmp / "AGENTS.md").write_text(agents)
    (tmp / "README.md").write_text(readme)
    return tmp


def test_paths(tmp_path: Path) -> None:
    root = _repo(tmp_path, "see `engine.py`, `app/engine.py`, `docs/adr/NNNN-x.md` and `gone.py`, [x](nope.md)")
    assert check_docs.check_paths(root) == ["AGENTS.md: missing path gone.py", "AGENTS.md: missing path nope.md"]


def test_options() -> None:
    known = check_docs.cli_options(cli._parser())
    assert {"--format", "--view", "--pace", "--transcript", "--config"} <= known
    root = ROOT
    assert check_docs.check_options(known, root) == []


def test_options_drift(tmp_path: Path) -> None:
    root = _repo(tmp_path, "run `duel run --bogus` or `pytest --cov`\n", "duel run --format json --nope\n")
    assert check_docs.check_options({"--format"}, root) == [
        "AGENTS.md: unknown duel option --bogus",
        "README.md: unknown duel option --nope",
    ]


def test_env(tmp_path: Path) -> None:
    root = _repo(tmp_path, "", "DUEL_MODE only")
    assert check_docs.check_env(("DUEL_MODE", "DUEL_X"), root) == ["README.md: env var DUEL_X not documented"]
    assert check_docs.check_env(config.ENV_VARS) == []


def test_modules(tmp_path: Path) -> None:
    root = _repo(tmp_path, "nothing")
    assert check_docs.check_modules(root) == [
        "AGENTS.md: port Sandbox not named",
        "AGENTS.md: module app/engine.py not named",
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_links(tmp_path: Path) -> None:
    assert check_docs.check_links(tmp_path) == [
        "CLAUDE.md must be a symlink to AGENTS.md",
        ".cursor/skills must be a symlink to ../.agents/skills",
    ]
    (tmp_path / ".cursor").mkdir()
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    (tmp_path / ".cursor" / "skills").symlink_to("../.agents/skills")
    assert check_docs.check_links(tmp_path) == []
