"""Release gate H1: tracked files must not leak maintainer host paths."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_HOST = re.compile("/" + "(?:workspace|home/box)\\b|/" + "tmp/cd-")


def test_no_host_paths_in_tracked_files() -> None:
    names = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for name in names:
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        hits += [f"{name}:{n}" for n, line in enumerate(text.splitlines(), 1) if _HOST.search(line)]
    assert hits == []
