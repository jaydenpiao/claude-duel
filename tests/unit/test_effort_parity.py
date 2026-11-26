"""One reasoning effort for every model in a ladder plan: each vendor adapter sends exactly the plan's effort
(fake transports only, no live call), so no model runs on a vendor or small-model default."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from claude_duel.adapters.providers.anthropic import AnthropicProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.app import ladder_config as lc
from claude_duel.app.budget import vendor_of
from claude_duel.domain.errors import ConfigError
from claude_duel.ports import CompletionRequest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
CLAUDE = {"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 5, "output_tokens": 5}}
GPT = {
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
    "usage": {"input_tokens": 5, "output_tokens": 5},
}


def _sent_effort(model: str, effort: str | None) -> object:
    """The effort the real adapter puts on the wire for ``model`` when the request carries ``effort``."""
    bodies: list[dict[str, object]] = []

    def h(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=CLAUDE if vendor_of(model) == "anthropic" else GPT)

    transport = httpx.MockTransport(h)
    req = CompletionRequest(system="s", prompt="p", purpose="build", max_tokens=100, reasoning_effort=effort)
    if vendor_of(model) == "anthropic":
        AnthropicProvider(SecretStr("sk-ant-test-0000"), model, transport=transport).complete(req)
        config = bodies[0].get("output_config")
        return config.get("effort") if isinstance(config, dict) else None
    OpenAIProvider(SecretStr("sk-test-0000000000"), model, transport=transport).complete(req)
    reasoning = bodies[0].get("reasoning")
    return reasoning.get("effort") if isinstance(reasoning, dict) else None


@pytest.mark.parametrize("plan", ["final", "pilot", "pilot-self"])
def test_every_model_in_a_plan_is_sent_the_same_effort(plan: str) -> None:
    cfg = lc.load(EXAMPLES / f"ladder-{plan}.toml", allow_premium=True)
    assert cfg.caps.effort == "medium"
    assert cfg.rules().reasoning_effort == "medium"
    models = {m for side in (cfg.sides.a, cfg.sides.b) for m in side}
    assert {m: _sent_effort(m, cfg.rules().reasoning_effort) for m in models} == dict.fromkeys(models, "medium")


def test_without_a_plan_effort_the_vendors_would_differ() -> None:
    """Why the plan pins it: left unset, gpt-6-luna gets "low" while Claude and the larger GPT models get none."""
    assert _sent_effort("gpt-6-luna", None) == "low"
    assert _sent_effort("claude-haiku-5-5", None) is None
    assert _sent_effort("gpt-6.2-astra", None) is None


def test_effort_defaults_to_medium_and_is_overridable_but_validated() -> None:
    plan = lc.load(EXAMPLES / "ladder-fake.toml")
    assert plan.rules().reasoning_effort == "medium"
    assert lc.load(EXAMPLES / "ladder-fake.toml", {"caps.effort": "high"}).rules().reasoning_effort == "high"
    with pytest.raises(ConfigError, match="effort"):
        lc.load(EXAMPLES / "ladder-fake.toml", {"caps.effort": "minimal"})
