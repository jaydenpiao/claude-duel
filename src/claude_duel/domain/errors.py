"""Domain and infrastructure error hierarchy."""


class DuelError(Exception):
    """Base error."""


class InvalidTransitionError(DuelError):
    """Illegal match lifecycle transition."""


class ProviderError(DuelError):
    """Base provider failure."""

    retryable: bool = False


class TransientProviderError(ProviderError):
    """5xx, connection reset, etc."""

    retryable = True


class ProviderTimeoutError(TransientProviderError):
    """Request timed out."""


class RateLimitError(TransientProviderError):
    """429 from provider."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentProviderError(ProviderError):
    """Non-retryable provider failure (bad request, auth)."""


class QuotaExceededError(PermanentProviderError):
    """Provider account is out of quota or funds. Never retried; aborts the match."""


class BudgetExceededError(DuelError):
    """Arena cost circuit breaker tripped."""


class MalformedOutputError(DuelError):
    """Model output could not be parsed."""


class FlagLeakError(DuelError):
    """A flag was about to reach an untrusted party."""


class MatchLockedError(DuelError):
    """Another process holds the match lock."""


class MatchNotFoundError(DuelError):
    """No persisted match with that id."""


class ConfigError(DuelError):
    """Invalid configuration."""


class EventLogCorruptError(DuelError):
    """The append-only event log was edited, truncated mid-chain or reordered."""


class SandboxRefusedError(DuelError):
    """A sandbox was asked to run code it must never run (e.g. model-written code outside Docker)."""


class DeadlineExceededError(DuelError):
    """The match used up ``rules.match_deadline_s`` of recorded provider time (#13)."""


class WalletEmptyError(DuelError):
    """An agent's per-match wallet (``rules.wallet``) is spent; its builds forfeit and its attacks stop."""
