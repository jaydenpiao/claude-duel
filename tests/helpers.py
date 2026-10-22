"""Test-only helpers."""


def any_source(source: str) -> bool:
    """Lets tests run arbitrary (exploit) code in the subprocess sandbox. Never used outside tests."""
    return bool(source)
