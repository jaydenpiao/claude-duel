from __future__ import annotations

import json
import random

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from claude_duel.adapters.providers.fake import TEMPLATES, FakeProvider
from claude_duel.adapters.providers.openai import OpenAIProvider
from claude_duel.adapters.providers.retry import RetryingProvider, backoff_delay
from claude_duel.domain.errors import (
    PermanentProviderError,
    ProviderTimeoutError,
    QuotaExceededError,
    RateLimitError,
    TransientProviderError,
)
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest

REQ = CompletionRequest(system="s", prompt="TASK: BUILD\nAGENT: a\nROUND: 1\nINDEX: 0", purpose="build")


class Scripted:
    name = "scripted"

    def __init__(self, *outcomes: Exception | str) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return Completion(text=o, input_tokens=1, output_tokens=1)


class TestRetry:
    def make(self, inner: Scripted, sleeps: list[float]) -> RetryingProvider:
        return RetryingProvider(
            inner,
            InMemoryMetrics(),
            max_retries=3,
            base_delay=1,
            max_delay=8,
            sleep=sleeps.append,
            rng=random.Random(0),
        )

    def test_retries_transient_then_succeeds(self) -> None:
        sleeps: list[float] = []
        inner = Scripted(ProviderTimeoutError("t"), TransientProviderError("5xx"), "ok")
        assert self.make(inner, sleeps).complete(REQ).text == "ok"
        assert inner.calls == 3
        assert len(sleeps) == 2

    def test_honours_retry_after(self) -> None:
        sleeps: list[float] = []
        self.make(Scripted(RateLimitError("429", retry_after=5), "ok"), sleeps).complete(REQ)
        assert sleeps[0] >= 5

    def test_gives_up(self) -> None:
        inner = Scripted(*[TransientProviderError("x")] * 4)
        with pytest.raises(TransientProviderError):
            self.make(inner, []).complete(REQ)
        assert inner.calls == 4

    def test_permanent_not_retried(self) -> None:
        inner = Scripted(PermanentProviderError("401"))
        with pytest.raises(PermanentProviderError):
            self.make(inner, []).complete(REQ)
        assert inner.calls == 1

    @given(st.integers(0, 30), st.floats(0.01, 5), st.floats(1, 60), st.integers())
    def test_backoff_bounded(self, attempt: int, base: float, cap: float, seed: int) -> None:
        d = backoff_delay(attempt, base, cap, random.Random(seed))
        assert 0 <= d <= cap


def _openai(handler: httpx.MockTransport) -> OpenAIProvider:
    return OpenAIProvider(SecretStr("sk-test-0000000000"), "m", transport=handler)


class TestOpenAI:
    def test_success_and_auth_header(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            assert req.headers["authorization"] == "Bearer sk-test-0000000000"
            assert json.loads(req.content)["model"] == "m"
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                },
            )

        c = _openai(httpx.MockTransport(h)).complete(REQ)
        assert (c.text, c.total_tokens) == ("{}", 7)

    @pytest.mark.parametrize(
        ("status", "headers", "exc"),
        [
            (429, {"retry-after": "2"}, RateLimitError),
            (429, {"retry-after": "soon"}, RateLimitError),
            (503, {}, TransientProviderError),
            (401, {}, PermanentProviderError),
        ],
    )
    def test_status_mapping(self, status: int, headers: dict[str, str], exc: type[Exception]) -> None:
        p = _openai(httpx.MockTransport(lambda r: httpx.Response(status, headers=headers)))
        with pytest.raises(exc):
            p.complete(REQ)

    def test_quota_is_permanent(self) -> None:
        body = {"error": {"code": "insufficient_quota"}}
        p = _openai(httpx.MockTransport(lambda r: httpx.Response(429, json=body)))
        with pytest.raises(QuotaExceededError, match="spend limit"):
            p.complete(REQ)
        assert not QuotaExceededError.retryable

    def test_timeout_and_transport(self) -> None:
        def timeout(r: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        def reset(r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("reset")

        with pytest.raises(ProviderTimeoutError):
            _openai(httpx.MockTransport(timeout)).complete(REQ)
        with pytest.raises(TransientProviderError):
            _openai(httpx.MockTransport(reset)).complete(REQ)

    def test_unexpected_body(self) -> None:
        p = _openai(httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1})))
        with pytest.raises(TransientProviderError):
            p.complete(REQ)


class TestFake:
    def test_deterministic(self) -> None:
        assert FakeProvider().complete(REQ) == FakeProvider().complete(REQ)

    def test_malformed_then_valid(self) -> None:
        p = FakeProvider(malformed_first=1)
        assert "{" not in p.complete(REQ).text
        assert "{" in p.complete(REQ).text

    def test_attacker_exhausts_knowledge(self) -> None:
        req = CompletionRequest(system="s", prompt="TASK: ATTACK\nAGENT: a\nATTEMPT: 99", purpose="attack")
        assert "guess-99" in FakeProvider(skill=len(TEMPLATES)).complete(req).text
