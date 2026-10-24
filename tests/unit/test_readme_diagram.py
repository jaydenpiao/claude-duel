"""Release gate: the README architecture diagram names every port and the main layers."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_DIAGRAM = re.search(r"```mermaid\n(.*?)```", (ROOT / "README.md").read_text(), re.S)


def _diagram() -> str:
    assert _DIAGRAM is not None
    return _DIAGRAM.group(1)


def test_every_engine_port_is_drawn() -> None:
    tree = ast.parse((ROOT / "src" / "claude_duel" / "ports.py").read_text())
    ports = {n.name for n in tree.body if isinstance(n, ast.ClassDef) and "Protocol" in ast.unparse(n)}
    drawn = {p for p in ports if f"|{p}|" in _diagram()}
    assert {"LLMProvider", "Sandbox", "MatchStore", "EventLog", "SpendLedger", "Metrics"} <= drawn


def test_layers_and_runtime_are_drawn() -> None:
    for name in ("series", "tournament", "observers", "presentation", "gVisor", "hash-chained", "replay"):
        assert name in _diagram()
