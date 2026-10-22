"""Retry decorator: exponential backoff with full jitter."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from claude_duel.domain.errors import ProviderError, RateLimitError
from claude_duel.observability import log
from claude_duel.ports import Completion, CompletionRequest, LLMProvider, Metrics

_LOG = logging.getLogger(__name__)


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Full-jitter delay for a zero-based attempt number."""
    return rng.uniform(0, min(cap, base * (2**attempt)))


class RetryingProvider:
    def __init__(
        self,
        inner: LLMProvider,
        metrics: Metrics,
        max_retries: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.name = inner.name
        self._inner = inner
        self._metrics = metrics
        self._max_retries = max_retries
        self._base = base_delay
        self._cap = max_delay
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not crypto

    def complete(self, request: CompletionRequest) -> Completion:
        attempt = 0
        while True:
            try:
                return self._inner.complete(request)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    self._metrics.incr("provider.failure", provider=self.name, kind=type(exc).__name__)
                    raise
                delay = backoff_delay(attempt, self._base, self._cap, self._rng)
                if isinstance(exc, RateLimitError) and exc.retry_after is not None:
                    delay = max(delay, min(exc.retry_after, self._cap))
                self._metrics.incr("provider.retry", provider=self.name, kind=type(exc).__name__)
                log(
                    _LOG,
                    logging.WARNING,
                    "provider retry",
                    provider=self.name,
                    attempt=attempt,
                    delay=delay,
                    error=type(exc).__name__,
                )
                self._sleep(delay)
                attempt += 1
