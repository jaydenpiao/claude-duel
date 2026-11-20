from __future__ import annotations

import hashlib
import json
import os
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from claude_duel.adapters.providers import openai as openai_mod
from claude_duel.adapters.providers.fake import TEMPLATES, FakeProvider
from claude_duel.adapters.providers.openai import OpenAIProvider, default_safety_identifier
from claude_duel.adapters.providers.registry import FACTORIES
from claude_duel.adapters.providers.retry import RetryingProvider, backoff_delay
from claude_duel.app.container import make_provider
from claude_duel.config import load_settings
from claude_duel.domain.errors import (
    CyberPolicyError,
    PermanentProviderError,
    ProviderTimeoutError,
    QuotaExceededError,
    RateLimitError,
    TransientProviderError,
)
from claude_duel.domain.models import AgentSpec
from claude_duel.observability import InMemoryMetrics
from claude_duel.ports import Completion, CompletionRequest

_REAL_INSTALL_PATH = openai_mod.install_id_path  # captured before the conftest fixture patches it
AS_ROOT = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores dir modes")

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

    def test_cyber_policy_not_retried(self) -> None:
        inner = Scripted(CyberPolicyError("cyber_policy"))
        with pytest.raises(CyberPolicyError):
            self.make(inner, []).complete(REQ)
        assert inner.calls == 1

    @given(st.integers(0, 30), st.floats(0.01, 5), st.floats(1, 60), st.integers())
    def test_backoff_bounded(self, attempt: int, base: float, cap: float, seed: int) -> None:
        d = backoff_delay(attempt, base, cap, random.Random(seed))
        assert 0 <= d <= cap


def _openai(handler: httpx.MockTransport) -> OpenAIProvider:
    return OpenAIProvider(SecretStr("sk-test-0000000000"), "m", transport=handler, safety_identifier="id-for-tests")


class TestOpenAI:
    def test_success_and_auth_header(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            assert req.headers["authorization"] == "Bearer sk-test-0000000000"
            body = json.loads(req.content)
            assert (body["model"], body["temperature"]) == ("m", 0.0)
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

    def test_cyber_policy_is_permanent(self) -> None:
        body = {"error": {"code": "cyber_policy", "message": "access limited"}}
        p = _openai(httpx.MockTransport(lambda r: httpx.Response(403, json=body)))
        with pytest.raises(CyberPolicyError, match="not retried"):
            p.complete(REQ)
        assert not CyberPolicyError.retryable
        assert issubclass(CyberPolicyError, PermanentProviderError)

    def test_chat_and_responses_send_safety_identifier(self) -> None:
        seen: list[dict[str, object]] = []

        def h(req: httpx.Request) -> httpx.Response:
            seen.append(json.loads(req.content))
            if req.url.path.endswith("/responses"):
                return httpx.Response(200, json={"output": [], "usage": {}})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{}"}}], "usage": {}},
            )

        _openai(httpx.MockTransport(h)).complete(REQ)
        OpenAIProvider(
            SecretStr("sk-test-0000000000"),
            "gpt-6-luna",
            transport=httpx.MockTransport(h),
            safety_identifier="id-for-tests",
        ).complete(REQ)
        assert all(s["safety_identifier"] == "id-for-tests" for s in seen)
        assert len(seen) == 2

    def test_install_id_is_stable_private_and_never_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "store" / "install-id"
        first = default_safety_identifier(path)
        assert first == default_safety_identifier(path)
        assert path.read_text().strip()
        assert path.stat().st_mode & 0o777 == 0o600
        path.write_text("")  # an empty file counts as missing
        assert default_safety_identifier(path) not in {first, hashlib.sha256(b"").hexdigest()}

    def test_concurrent_creation_yields_one_id(self, tmp_path: Path) -> None:
        path = tmp_path / "install-id"
        with ThreadPoolExecutor(8) as pool:
            ids = set(pool.map(lambda _: default_safety_identifier(path), range(16)))
        assert len(ids) == 1
        assert hashlib.sha256(b"").hexdigest() not in ids

    @AS_ROOT
    def test_unwritable_store_falls_back_without_raising(self, tmp_path: Path) -> None:
        locked = tmp_path / "ro"
        locked.mkdir(mode=0o500)
        try:
            sid = default_safety_identifier(locked / "sub" / "install-id")
            assert sid == default_safety_identifier(locked / "sub" / "install-id")  # per-process fallback
            assert sid != hashlib.sha256(b"").hexdigest()
        finally:
            locked.chmod(0o700)

    @AS_ROOT
    def test_unwritable_home_does_not_raise(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(openai_mod, "install_id_path", _REAL_INSTALL_PATH)
        home = tmp_path / "home"
        home.mkdir(mode=0o500)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        try:
            FACTORIES["openai"](SecretStr("sk-test-0000000000"), "gpt-6-luna", 60.0, tmp_path / "store")
            assert (tmp_path / "store" / "install-id").is_file()
            assert not any(home.iterdir())
        finally:
            home.chmod(0o700)

    def test_install_id_follows_a_toml_store_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(openai_mod, "install_id_path", _REAL_INSTALL_PATH)
        monkeypatch.chdir(tmp_path)
        cfg = tmp_path / "duel.toml"
        cfg.write_text(f'store_dir = "{tmp_path / "toml-store"}"\n')
        settings = load_settings(cfg, env={"OPENAI_API_KEY": "sk-test-0000000000"})
        make_provider(AgentSpec(name="a", provider="openai"), settings, InMemoryMetrics())
        assert (tmp_path / "toml-store" / "install-id").is_file()
        assert not (tmp_path / ".duel").exists()  # not the cwd default

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


class TestDeadline:
    def make(self, inner: Scripted, sleeps: list[float], now: list[float], deadline: float) -> RetryingProvider:
        def sleep(s: float) -> None:
            sleeps.append(s)
            now[0] += s

        return RetryingProvider(
            inner,
            InMemoryMetrics(),
            max_retries=10,
            base_delay=1,
            max_delay=8,
            sleep=sleep,
            rng=random.Random(0),
            deadline_s=deadline,
            clock=lambda: now[0],
        )

    def test_retry_after_above_cap_fails_fast(self) -> None:
        sleeps: list[float] = []
        inner = Scripted(RateLimitError("429", retry_after=60), "ok")
        with pytest.raises(RateLimitError):
            self.make(inner, sleeps, [0.0], 120).complete(REQ)
        assert (sleeps, inner.calls) == ([], 1)

    def test_stops_before_passing_the_call_deadline(self) -> None:
        sleeps: list[float] = []
        inner = Scripted(*[RateLimitError("429", retry_after=4)] * 10, "ok")
        with pytest.raises(RateLimitError):
            self.make(inner, sleeps, [0.0], 10).complete(REQ)
        assert sum(sleeps) <= 10
        assert inner.calls < 10

    def test_within_deadline_still_retries(self) -> None:
        sleeps: list[float] = []
        inner = Scripted(RateLimitError("429", retry_after=2), "ok")
        assert self.make(inner, sleeps, [0.0], 10).complete(REQ).text == "ok"


class TestSafetyIdentifier:
    def test_stable_hash_is_not_the_raw_id(self, tmp_path: Path) -> None:
        path = tmp_path / "install-id"
        first = default_safety_identifier(path)
        second = default_safety_identifier(path)
        raw = path.read_text().strip()
        assert first == second
        assert len(first) == 64
        assert first == hashlib.sha256(raw.encode()).hexdigest()
        assert first != raw
        assert len(raw) == 32
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_new_path_gets_a_different_id(self, tmp_path: Path) -> None:
        a = default_safety_identifier(tmp_path / "a")
        b = default_safety_identifier(tmp_path / "b")
        assert a != b
