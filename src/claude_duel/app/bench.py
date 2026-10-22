"""Benchmark slice harness (#45, docs/design/benchmark.md): solve rate on fixed stdin-only tasks.

Tasks are read from a local JSON-lines manifest and never vendored into the repo. Each task is an
arena-style program that prints ``FLAG`` for the right stdin. Every call goes through the match
``CostBudget`` and the shared spend ledger (#104), with the whole bench run counted as one match.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_duel.app import agents
from claude_duel.app.budget import CostBudget
from claude_duel.app.container import build_budget, make_provider, make_sandbox
from claude_duel.app.engine import MALFORMED
from claude_duel.config import Settings
from claude_duel.domain.errors import ConfigError, MalformedOutputError
from claude_duel.domain.flags import derive_flag
from claude_duel.domain.models import AgentSpec, MatchState, Rules, SandboxLimits, Usage
from claude_duel.observability import NullMetrics
from claude_duel.ports import Completion, CompletionRequest, LLMProvider, Sandbox


class MeteredProvider:
    """Checks the budget caps before each call and charges the ledger after it, exactly as a match does."""

    def __init__(self, inner: LLMProvider, model: str, budget: CostBudget, state: MatchState) -> None:
        self._inner, self._model, self._budget, self._state = inner, model, budget, state
        self.name = inner.name

    def complete(self, request: CompletionRequest) -> Completion:
        self._budget.check(self._state)
        completion = self._inner.complete(request)
        usd = self._budget.charge(self._state.match_id, self._model, completion)
        usage = self._state.usage
        usage.calls, usage.tokens, usage.usd = usage.calls + 1, usage.tokens + completion.total_tokens, usage.usd + usd
        return completion


class BenchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    source: str = Field(min_length=1)
    origin: str = Field(min_length=1)  # where the original task came from (URL or "local")
    license: str = Field(min_length=1)


class BenchResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    task_id: str
    origin: str  # provenance travels with every result (licensing, see the design doc)
    license: str
    agent: str
    solved: bool
    attempts: int
    tokens: int


def load_tasks(path: Path) -> list[BenchTask]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        tasks = [BenchTask.model_validate(json.loads(line)) for line in lines if line.strip()]
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(f"bad bench manifest {path}: {exc}") from exc
    if len({t.id for t in tasks}) != len(tasks):
        raise ConfigError(f"bad bench manifest {path}: duplicate task ids")
    return tasks


def solve(
    task: BenchTask, agent: AgentSpec, provider: LLMProvider, sandbox: Sandbox, limits: SandboxLimits, tries: int
) -> BenchResult:
    """Same attack prompt, parser and sandbox as a match, so bench and duel numbers are comparable."""
    flag = derive_flag(secrets.token_hex(16), task.id)  # fresh per solve; never in a prompt
    history: list[tuple[str, str]] = []
    tokens = 0
    for n in range(1, tries + 1):
        prompt = agents.attack_prompt(agent.name, task.id, task.title, task.description, task.source, n, history)
        completion = provider.complete(
            CompletionRequest(system=agents.system_prompt(""), prompt=prompt, purpose="attack")
        )
        tokens += completion.total_tokens
        try:
            payload = agents.parse_attack(completion.text).payload
        except MalformedOutputError as exc:
            history.append((MALFORMED, str(exc)))
            continue
        out = sandbox.run(task.source, payload, flag, limits).stdout
        if flag in out:
            return BenchResult(
                task_id=task.id,
                origin=task.origin,
                license=task.license,
                agent=agent.name,
                solved=True,
                attempts=n,
                tokens=tokens,
            )
        history.append((payload, out.replace(flag, "")[:500]))
    return BenchResult(
        task_id=task.id,
        origin=task.origin,
        license=task.license,
        agent=agent.name,
        solved=False,
        attempts=tries,
        tokens=tokens,
    )


def solve_rate(results: list[BenchResult]) -> dict[str, float]:
    by_agent: dict[str, list[bool]] = {}
    for r in results:
        by_agent.setdefault(r.agent, []).append(r.solved)
    return {a: round(sum(v) / len(v), 3) for a, v in sorted(by_agent.items())}


def run_bench(settings: Settings, manifest: Path, tries: int, bench_id: str) -> tuple[list[BenchResult], Usage]:
    """Both configured agents on every task, under the budget caps (match caps apply to the whole run).

    Real models only run in the Docker sandbox (``make_sandbox`` fails closed). A cap stops the run with
    ``BudgetExceededError``; spend so far is already in the ledger under ``bench_id``.
    """
    tasks = load_tasks(manifest)
    sandbox = make_sandbox(settings, settings.agents)
    budget = build_budget(settings)
    budget.check_lifetime()
    state = MatchState(match_id=bench_id, agents=settings.agents, rules=Rules(), limits=settings.limits)
    results = []
    for agent in settings.agents:
        inner = make_provider(agent, settings, NullMetrics())
        provider = MeteredProvider(inner, settings.model_for(agent), budget, state)
        results += [solve(t, agent, provider, sandbox, settings.limits, tries) for t in tasks]
    return results, state.usage
