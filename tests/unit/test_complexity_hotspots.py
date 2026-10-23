"""Quality: the former radon-C hotspots stay at B (cyclomatic complexity <= 10, counted as radon does)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "claude_duel"
_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.Assert, ast.comprehension)


def _complexity(fn: ast.AST) -> int:
    score = 1
    for node in ast.walk(fn):
        if isinstance(node, _BRANCHES):
            score += 1 + (len(node.ifs) if isinstance(node, ast.comprehension) else 0)
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
    return score


def _function(path: str, name: str) -> ast.AST:
    tree = ast.parse((SRC / path).read_text())
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("domain/contract.py", "compose"),
        ("domain/contract.py", "_rebinds"),
        ("domain/contract.py", "_problem"),
        ("domain/ratings.py", "bradley_terry"),
        ("app/engine.py", "_author"),
        ("cli.py", "_series"),
    ],
)
def test_hotspot_stays_simple(path: str, name: str) -> None:
    assert _complexity(_function(path, name)) <= 10
