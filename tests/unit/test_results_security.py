"""Security review fixes for duel sync: TLS for remote hosts (M1) and encoded-flag detection (L1)."""

from __future__ import annotations

import codecs

import pytest

from claude_duel.adapters.store.postgres import require_tls
from claude_duel.domain.errors import ConfigError, FlagLeakError
from claude_duel.domain.flags import assert_no_flag, flag_shaped, scrub
from claude_duel.domain.public import FREE_TEXT, public_text
from tests.helpers import FLAG


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@localhost/db",
        "postgresql://u:p@127.0.0.1:55432/db",
        "postgresql://u:p@[::1]/db",
        "host=/var/run/postgresql dbname=db",
        "postgresql://u:p@db.example.com/db?sslmode=require",
        "postgresql://u:p@db.example.com/db?sslmode=verify-full",
    ],
)
def test_tls_accepted(url: str) -> None:
    require_tls(url, {})


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db.example.com/db",
        "postgresql://u:p@db.example.com/db?sslmode=prefer",
        "postgresql://u:p@db.example.com/db?sslmode=disable",
        "postgresql://u:p@localhost,db.example.com/db",
    ],
)
def test_tls_refused_for_remote_hosts(url: str) -> None:
    with pytest.raises(ConfigError, match="sslmode") as err:
        require_tls(url, {})
    assert "u:p" not in str(err.value)


def test_tls_reads_libpq_env() -> None:
    require_tls("dbname=db", {"PGHOST": "db.example.com", "PGSSLMODE": "verify-ca"})
    with pytest.raises(ConfigError):
        require_tls("dbname=db", {"PGHOST": "db.example.com"})


def test_malformed_url_is_not_echoed() -> None:
    with pytest.raises(ConfigError, match="malformed") as err:
        require_tls("postgresql://u:hunter2@h:notaport/db?x=", {})
    assert "hunter2" not in str(err.value)


@pytest.mark.parametrize(
    "variant",
    [
        codecs.encode(FLAG, "rot13"),
        " ".join(str(b) for b in FLAG.encode()),
        ",".join(str(b) for b in FLAG.encode()),
    ],
)
def test_encoded_flags_are_caught(variant: str) -> None:
    with pytest.raises(FlagLeakError):
        assert_no_flag(f"out: {variant}", FLAG, "test")
    assert flag_shaped(variant)
    assert variant not in scrub(variant)
    assert variant not in scrub(variant, (FLAG,))


def test_public_text_keeps_tokens_only() -> None:
    assert public_text("timeout") == "timeout"
    assert public_text("anthropic:claude-x") == "anthropic:claude-x"
    assert public_text("model printed the flag here") == FREE_TEXT
    assert FLAG not in public_text(FLAG)
