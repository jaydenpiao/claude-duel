"""`duel ladder` with fake agents: deterministic, resumable, verified, capped, and refused before any call."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from claude_duel.adapters.providers.fake import FakeProvider
from claude_duel.adapters.store.ledger import FileSpendLedger
from claude_duel.app import ladder as ladder_mod
from claude_duel.app import ladder_config as lc
from claude_duel.app.budget import DEFAULT_PRICES, FAKE_MODEL, BudgetLimits, CostBudget
from claude_duel.app.container import build_ladder_log, build_ledger
from claude_duel.app.ladder import Projection, play_ladder, verify_ladder
from claude_duel.cli import main
from claude_duel.config import Settings
from claude_duel.domain import ladder as ld
from claude_duel.domain.errors import BudgetExceededError, ConfigError
from claude_duel.domain.models import CallCaps, MatchState
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FAKE = EXAMPLES / "ladder-fake.toml"


def _settings(root: Path) -> Settings:
    return Settings(store_dir=root, sandbox="subprocess")


def _summary(state: ld.LadderState) -> list[tuple[str, str, str]]:
    return [(s.a, s.b, s.result) for s in state.steps]


def test_fake_ladder_is_deterministic_resumable_and_verified(tmp_path: Path) -> None:
    cfg = lc.load(FAKE)
    one = play_ladder(_settings(tmp_path / "x"), "lad-one", cfg, InMemoryMetrics())
    two = play_ladder(_settings(tmp_path / "y"), "lad-one", cfg, InMemoryMetrics())
    assert one.done()
    assert not one.capped
    assert _summary(one) == _summary(two)
    assert len(one.steps) >= 2
    again = play_ladder(_settings(tmp_path / "x"), "lad-one", cfg, InMemoryMetrics())
    assert again.steps == one.steps  # resume replays nothing
    assert verify_ladder(_settings(tmp_path / "x"), "lad-one").consistent


def test_another_plan_for_the_same_id_is_refused(tmp_path: Path) -> None:
    play_ladder(_settings(tmp_path), "lad-two", lc.load(FAKE), InMemoryMetrics())
    with pytest.raises(ConfigError, match="another plan"):
        play_ladder(_settings(tmp_path), "lad-two", lc.load(FAKE, {"match.boards_min": 1}), InMemoryMetrics())


def test_cap_stops_before_a_step_that_could_pass_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder_mod, "project", lambda cfg: Projection([], 0.0, []))
    monkeypatch.setattr(ladder_mod, "match_usd", lambda cfg, a, b, worst=True: 1.5)
    state = play_ladder(_settings(tmp_path), "lad-three", lc.load(FAKE), InMemoryMetrics())
    assert state.capped
    assert state.steps == []
    assert ld.fold("lad-three", build_ladder_log(_settings(tmp_path)).read("lad-three")).capped


def test_over_cap_plan_makes_no_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(FakeProvider, "complete", lambda self, req: calls.append(req.purpose))
    plan = str(EXAMPLES / "ladder-a.toml")
    assert main(["ladder", plan, "--ladder-max-usd", "30", "--match-max-usd", "30"]) == 1
    out = capsys.readouterr().out
    assert "refused: ladder worst case $" in out
    assert calls == []
    assert not list(tmp_path.rglob("*.ladder.jsonl"))
    assert build_ledger(_settings(tmp_path / ".duel")).spent_usd("") == 0


def test_cli_dry_run_and_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.toml").write_text('sandbox = "subprocess"\n')
    assert main(["ladder", str(FAKE), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run ok" in out
    assert "typical ladder $0.00" in out
    assert main(["ladder", str(FAKE), "--dry-run", "--format", "json", "--boards-max", "3"]) == 0
    out = capsys.readouterr().out
    assert '"refusals": []' in out
    assert '"typical_usd": 0.0' in out
    assert main(["--config", "c.toml", "ladder", str(FAKE), "--ladder-id", "lad-four"]) == 0
    out = capsys.readouterr().out
    assert "ladder lad-four" in out
    assert "verified ✓" in out
    assert main(["--config", "c.toml", "ladder", str(FAKE), "--ladder-id", "lad-four", "--format", "json"]) == 0
    assert '"verified": true' in capsys.readouterr().out
    assert main(["ladder", str(FAKE), "--run-date", "11/20/2026", "--dry-run"]) == 2


class _Caps(FakeProvider):
    def __init__(self, seen: dict[str, int]) -> None:
        super().__init__()
        self.seen = seen

    def complete(self, request: CompletionRequest) -> Completion:
        self.seen[request.purpose] = request.max_tokens
        return super().complete(request)


def test_engine_sends_the_per_purpose_cap(make_engine: Callable, new_state: Callable) -> None:  # type: ignore[type-arg]
    seen: dict[str, int] = {}
    engine = make_engine({"alice": _Caps(seen), "bob": FakeProvider()})
    state: MatchState = new_state(rounds=1, challenges_per_round=1)
    caps = CallCaps(build=900, allocate=300, attack=700)
    state = state.model_copy(update={"rules": state.rules.model_copy(update={"call_caps": caps})})
    engine.run(state)
    assert seen["build"] == 900
    assert seen["attack"] <= 700


def test_hard_stop_ends_a_typical_basis_ladder_cleanly_and_a_raised_cap_resumes_it(tmp_path: Path) -> None:

    settings = _settings(tmp_path)
    prices = {**DEFAULT_PRICES, FAKE_MODEL: (1.0, 2.0)}  # priced fake calls, so the dollar caps bite

    def budget() -> CostBudget:
        return CostBudget(BudgetLimits(match_max_usd=10, run_max_usd=10), build_ledger(settings), prices)

    tight = lc.load(FAKE, {"budget.match_max_usd": 0.02, "budget.basis": "typical"})
    with pytest.raises(BudgetExceededError, match="hard stop"):
        play_ladder(settings, "lad-stop", tight, InMemoryMetrics(), budget=budget())
    assert FileSpendLedger(settings.store_dir / "ledger.jsonl").spent_usd("lad-stop-s1-") <= 0.02
    done = play_ladder(settings, "lad-stop", lc.load(FAKE), InMemoryMetrics(), budget=budget())  # same plan digest
    assert done.done()
    assert verify_ladder(settings, "lad-stop").consistent


def test_compare_marks_agreement_and_too_close() -> None:
    def run(lid: str, results: list[str]) -> ld.LadderState:
        state = ld.LadderState(ladder_id=lid, config={}, digest="d", a=("x1", "x2"), b=("y1", "y2"), start=(0, 0))
        for n, r in enumerate(results, 1):
            a, b = state.next_pair()
            state.steps.append(ld.Step(step_no=n, a=a, b=b, duplicate_id=f"{lid}-s{n}", result=r, usd=0))  # type: ignore[arg-type]
        return state

    same = ladder_mod.compare_ladders(run("r1", ["first", "first"]), run("r2", ["first", "first"]))
    assert [m.verdict for m in same.matchups] == ["agree", "agree"]
    assert same.ordering == "agree"
    split = ladder_mod.compare_ladders(run("r1", ["first", "first"]), run("r2", ["second", "first"]))
    assert split.ordering == "too close to call"
    assert split.matchups[0].verdict == "too close to call"
    assert split.matchups[0].results == ("first", "second")


def test_final_plan_passes_on_typical_cost_and_is_refused_on_worst() -> None:
    final = lc.load(EXAMPLES / "ladder-final.toml")
    assert not ladder_mod.project(final).refusals
    assert ladder_mod.project(lc.load(EXAMPLES / "ladder-final.toml", {"budget.basis": "worst"})).refusals
    assert final.digest() == lc.load(EXAMPLES / "ladder-final.toml", {"budget.ladder_max_usd": 200}).digest()


def test_watch_prints_halves_and_attacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["ladder", str(FAKE), "--ladder-id", "w1", "--watch", "--boards-max", "1", "--boards-min", "1"]) == 0
    out = capsys.readouterr().out
    assert "half 1: fake-1-a vs fake-1-b" in out
    assert "half done · $" in out
    assert "verified ✓" in out
