"""Retry decorator: exponential backoff with full jitter."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from claude_duel.domain.errors import ProviderError, ProviderTimeoutError, RateLimitError
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
        deadline_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = inner.name
        self._inner = inner
        self._metrics = metrics
        self._max_retries = max_retries
        self._base = base_delay
        self._cap = max_delay
        self._sleep = sleep
        self._deadline = deadline_s
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not crypto

    def complete(self, request: CompletionRequest) -> Completion:
        attempt, started = 0, self._clock()
        while True:
            try:
                return self._inner.complete(request)
            except ProviderError as exc:
                # a timeout is likely billed, so the engine retries it under a fresh reservation (S17-1)
                if not exc.retryable or isinstance(exc, ProviderTimeoutError) or attempt >= self._max_retries:
                    self._metrics.incr("provider.failure", provider=self.name, kind=type(exc).__name__)
                    raise
                delay = self._delay(attempt, exc, self._clock() - started)
                if delay is None:
                    self._metrics.incr("provider.deadline", provider=self.name)
                    raise
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

    def _delay(self, attempt: int, exc: ProviderError, elapsed: float) -> float | None:
        """Backoff for the next try, or None to fail fast: retry-after above the cap or past the call deadline."""
        delay = backoff_delay(attempt, self._base, self._cap, self._rng)
        if isinstance(exc, RateLimitError) and exc.retry_after is not None:
            if exc.retry_after > self._cap:
                return None
            delay = max(delay, exc.retry_after)
        return None if elapsed + delay > self._deadline else delay
