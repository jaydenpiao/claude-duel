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
