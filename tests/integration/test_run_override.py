"""Run-cap override for `duel experiment stakes` (design-stakes-modes.md): guard rails, audit trail, no leaks."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claude_duel.app.budget import RUN_OVERRIDE_CEILING_USD, BudgetLimits, make_override
from claude_duel.app.container import build_budget, build_event_log, build_ledger
from claude_duel.app.experiment import footer, run_stakes
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError
from claude_duel.domain.models import Rules
from claude_duel.observability import InMemoryMetrics

REASON = "stakes prereg, fake models"


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess", rules=Rules(rounds=1))


def _prereg(root: Path) -> Path:
    path = root / "prereg.md"
    path.write_text("# prereg\n")
    return path


@pytest.mark.parametrize(
    ("field", "value", "reason", "spent", "match"),
    [
        ("run_max_usd", RUN_OVERRIDE_CEILING_USD + 0.01, REASON, 0.0, "ceiling"),
        ("run_max_usd", 0.10, REASON, 0.0, "below the default"),
        ("run_max_usd", 1.00, "  ", 0.0, "reason is required"),
        ("match_max_usd", 1.00, REASON, 0.0, "only run_max_usd"),
        ("run_max_usd", 2.00, REASON, 38.5, "lifetime"),
    ],
)
def test_guard_rails(field: str, value: float, reason: str, spent: float, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        make_override(BudgetLimits(), field, value, reason, spent)


def test_override_is_a_copy_and_defaults_do_not_change(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    over = make_override(settings.budget, "run_max_usd", 2.0, REASON, 0.0)
    raised, plain = build_budget(settings, over), build_budget(settings)
    assert raised.limits.run_max_usd == 2.0
    assert plain.limits.run_max_usd == 0.50
    assert settings.budget.run_max_usd == 0.50
    assert raised.limits.match_max_usd == plain.limits.match_max_usd


def test_missing_prereg_refuses_before_any_call(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="pre-registration"):
        run_stakes(_settings(tmp_path), tmp_path / "nope.md", 1, InMemoryMetrics(), 2.0, REASON)
    assert build_ledger(_settings(tmp_path)).total_usd() == 0.0


def test_override_audit_trail(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(tmp_path)
    with caplog.at_level(logging.WARNING):
        result = run_stakes(settings, _prereg(tmp_path), 1, InMemoryMetrics(), 2.0, REASON)
    assert any("budget override: run_max_usd 0.50 -> 2.00" in r.getMessage() for r in caplog.records)
    assert [m["framing"] for m in result["matches"]] == ["neutral", "competitive", "survival"]
    assert {m["seed"] for m in result["matches"]} == {"stakes-00"}
    assert result["budget"]["overrides"][0]["value"] == 2.0
    assert footer(result) == f"run cap overridden to $2.00 ({REASON})"
    for m in result["matches"]:
        types = [e.type for e in build_event_log(settings).read(m["match_id"])]
        assert types[:2] == ["match_created", "budget_overridden"]
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    overrides = [r for r in rows if r.get("kind") == "override"]
    assert overrides == [{**overrides[0], "run_id": result["run_id"], "value": 2.0, "reason": REASON, "usd": 0.0}]


def test_without_override_no_audit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    result = run_stakes(settings, _prereg(tmp_path), 1, InMemoryMetrics())
    assert result["budget"]["overrides"] == []
    assert footer(result) == ""
    types = [e.type for e in build_event_log(settings).read(result["matches"][0]["match_id"])]
    assert "budget_overridden" not in types


def test_cli_json_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'store_dir = "{tmp_path}"\nsandbox = "subprocess"\n[rules]\nrounds = 1\n')
    base = ["--config", str(cfg), "experiment", "stakes", "--prereg", str(_prereg(tmp_path))]
    assert main([*base, "--run-max-usd", "2.0", "--override-reason", REASON, "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["budget"]["overrides"][0]["reason"] == REASON
    assert main([*base, "--run-max-usd", "2.0"]) == 2
    assert main([*base, "--override-reason", REASON]) == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -0.0])
def test_non_finite_or_negative_zero_override_is_refused(value: float) -> None:
    with pytest.raises(ConfigError):
        make_override(BudgetLimits(), "run_max_usd", value, REASON, 0.0)


def test_built_limits_are_validated(tmp_path: Path) -> None:
    override = make_override(BudgetLimits(), "run_max_usd", 1.0, REASON, 0.0)
    nan = override.model_copy(update={"value": float("nan")})  # bypasses make_override
    with pytest.raises(ValueError):
        build_budget(_settings(tmp_path), nan)
