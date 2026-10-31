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
        "AGENTS.md: app module engine.py missing from the app summary",
        "AGENTS.md: module app/engine.py not named",
    ]


def test_app_summary_names_every_app_module(tmp_path: Path) -> None:
    """An app module named only in a sub-AGENTS.md still fails: the root app summary must list it."""
    root = _repo(tmp_path, "- `src/claude_duel/app/`: `other.py`\n`Sandbox` `engine.py`")
    assert check_docs.check_modules(root) == ["AGENTS.md: app module engine.py missing from the app summary"]
    (root / "AGENTS.md").write_text("- `src/claude_duel/app/`: `engine.py`\n`Sandbox`")
    assert check_docs.check_modules(root) == []


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


def test_review_codes_rejected(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    # built at runtime so this file passes its own check
    forms = {
        "src/a.py": "R3" + "-2",
        "tests/b.md": "M" + "2",
        "docs/c.md": "Arch round " + "3",
        "src/d.py": "arch r" + "3 #8",
        "tests/e.py": "S" + "4-3",
    }
    for rel, code in forms.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"note {code} here\n")
    errors = check_docs.check_codes(tmp_path)
    assert len(errors) == len(forms)
    joined = "\n".join(errors)
    for rel in forms:
        assert rel in joined


def test_unpinned_action_is_caught(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    sha = "a" * 40
    wf.write_text(f"steps:\n  - uses: actions/checkout@v4\n  - uses: actions/setup-uv@{sha} # v1\n  - uses: ./local\n")
    errors = check_docs.check_pins(tmp_path)
    assert len(errors) == 1
    assert "actions/checkout@v4" in errors[0]
