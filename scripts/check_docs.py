"""Doc-drift gate (CI): fail when the agent docs and README stop matching the code.

Checks: (a) backticked repo paths and relative links exist; (b) every `duel ... --option` named in the
docs is a real CLI option; (c) every DUEL_* env var is in README; (d) every port Protocol and every
module is named in its AGENTS.md; (e) CLAUDE.md and .cursor/skills are symlinks; (f) every
workflow action is pinned to a full commit SHA. Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "claude_duel"
_PATH = re.compile(r"`([\w./-]+\.(?:py|md|toml|ya?ml|json|txt|tape|gif|svg))`")
_LINK = re.compile(r"\]\(([^)#\s]+)\)")
_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.M)
_PINNED = re.compile(r"@[0-9a-f]{40}$")
_DUEL_OPT = re.compile(r"`duel [^`]*`|^duel .*$", re.M)
_OPT = re.compile(r"(?<![\w-])--[a-z][a-z-]+")


def docs(root: Path = ROOT) -> list[Path]:
    found = [root / "AGENTS.md", root / "README.md", root / "tests" / "AGENTS.md"]
    found += sorted((root / "src").rglob("AGENTS.md")) + sorted((root / ".agents" / "skills").glob("*/SKILL.md"))
    return [p for p in found if p.exists()]


def _resolves(ref: str, doc: Path, root: Path) -> bool:
    if any(c in ref for c in "*<>{}") or "NNNN" in ref or ref.startswith(("http", "mailto:", ".duel/")):
        return True
    pkg = root / "src" / "claude_duel"
    candidates = [root / ref, doc.parent / ref, pkg / ref, root / "src" / ref]
    if any(c.exists() for c in candidates):
        return True
    if "/" not in ref:  # bare name: anywhere under the doc's directory or the package
        return any(next(base.rglob(ref), None) for base in (doc.parent, pkg, root / "tests", root / ".github"))
    return False


def check_paths(root: Path = ROOT) -> list[str]:
    errors = []
    for doc in docs(root):
        text = doc.read_text(encoding="utf-8")
        refs = set(_PATH.findall(text)) | {r for r in _LINK.findall(text) if not r.startswith("http")}
        errors += [
            f"{doc.relative_to(root)}: missing path {ref}" for ref in sorted(refs) if not _resolves(ref, doc, root)
        ]
    return errors


def cli_options(parser: argparse.ArgumentParser) -> set[str]:
    opts = {o for a in parser._actions for o in a.option_strings}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                opts |= cli_options(sub)
    return opts


def check_options(known: set[str], root: Path = ROOT) -> list[str]:
    errors = []
    for doc in (root / "AGENTS.md", root / "README.md"):
        for snippet in _DUEL_OPT.findall(doc.read_text(encoding="utf-8")):
            errors += [f"{doc.name}: unknown duel option {o}" for o in _OPT.findall(snippet) if o not in known]
    return sorted(set(errors))


def check_env(env_vars: tuple[str, ...], root: Path = ROOT) -> list[str]:
    readme = (root / "README.md").read_text(encoding="utf-8")
    return [f"README.md: env var {v} not documented" for v in env_vars if v not in readme]


def check_modules(root: Path = ROOT) -> list[str]:
    pkg = root / "src" / "claude_duel"
    top = (root / "AGENTS.md").read_text(encoding="utf-8")
    tree = ast.parse((pkg / "ports.py").read_text(encoding="utf-8"))
    protocols = [n.name for n in tree.body if isinstance(n, ast.ClassDef) and any(
        isinstance(b, ast.Name) and b.id == "Protocol" for b in n.bases)]  # fmt: skip
    errors = [f"AGENTS.md: port {p} not named" for p in protocols if f"`{p}`" not in top]
    for sub in sorted(p for p in pkg.iterdir() if p.is_dir() and not p.name.startswith("__")):
        doc = sub / "AGENTS.md" if (sub / "AGENTS.md").exists() else root / "AGENTS.md"
        text = doc.read_text(encoding="utf-8")
        for mod in sorted(sub.rglob("*.py")):
            if mod.name != "__init__.py" and mod.name not in text and mod.stem not in text:
                errors.append(f"{doc.relative_to(root)}: module {mod.relative_to(pkg)} not named")
    return errors


_CODE = re.compile(
    r"\bR\d-\d+\b|\b[BHML]\d\b|round-\d N\d|"
    r"(?i:arch round \d|arch #\d|\br\d+ #\d+|\bS\d-\d+\b)"
)


def check_codes(root: Path = ROOT) -> list[str]:
    """Private review codes (``R3-2``, ``arch round 3``, ``r3 #8``, ``S4-3``) explain nothing to outside readers."""
    files = sorted(f for d in ("src", "tests", "docs") for f in (root / d).rglob("*") if f.suffix in {".py", ".md"})
    return [
        f"{f.relative_to(root)}:{i}: review code {m.group()!r}; use the rationale, an issue or an ADR"
        for f in files
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if (m := _CODE.search(line))
    ]


def check_links(root: Path = ROOT) -> list[str]:
    want = {"CLAUDE.md": "AGENTS.md", ".cursor/skills": "../.agents/skills"}
    return [
        f"{name} must be a symlink to {target}"
        for name, target in want.items()
        if not (root / name).is_symlink() or str((root / name).readlink()) != target
    ]


def check_pins(root: Path = ROOT) -> list[str]:
    errors = []
    for wf in sorted((root / ".github").rglob("*.y*ml")):
        for ref in _USES.findall(wf.read_text()):
            if not ref.startswith(("./", "docker://")) and not _PINNED.search(ref):
                errors.append(f"{wf.relative_to(root)}: action not pinned to a commit SHA: {ref}")
    return errors


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from claude_duel import cli, config  # noqa: PLC0415 - import from this checkout, not site-packages

    errors = check_paths() + check_options(cli_options(cli._parser())) + check_env(config.ENV_VARS)
    errors += check_modules() + check_links() + check_codes() + check_pins()
    print("\n".join(errors) or "docs ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
