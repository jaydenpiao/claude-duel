"""Results DB connection strings (postgres_url) and the shared SQL helpers (results_sql), without a database."""

from __future__ import annotations

import pytest

from claude_duel.adapters.store.postgres_url import iam_url, require_tls
from claude_duel.domain.errors import ConfigError

IAM = "postgresql://duel-sql-proxy@project-0fedfc79-0519-43dd-8a0.iam@127.0.0.1:5433/duel"


def test_iam_url_escapes_the_iam_user_for_libpq() -> None:
    from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415

    info = conninfo_to_dict(iam_url(IAM))
    assert info["user"] == "duel-sql-proxy@project-0fedfc79-0519-43dd-8a0.iam"
    assert (info["host"], info["port"], info["dbname"]) == ("127.0.0.1", "5433", "duel")
    assert "password" not in info  # IAM auth through the proxy: no DB password
    require_tls(IAM, {})  # loopback proxy: TLS is the proxy's job
    assert iam_url("postgresql://u:p@127.0.0.1/db") == "postgresql://u:p@127.0.0.1/db"


def test_iam_url_over_the_proxy_unix_socket() -> None:
    from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415

    url = "postgresql://duel-ingest@project.iam@/duel?host=/run/duel-proxy/project:region:inst"
    info = conninfo_to_dict(iam_url(url))
    assert info["user"] == "duel-ingest@project.iam"
    assert (info["host"], info["dbname"]) == ("/run/duel-proxy/project:region:inst", "duel")
    require_tls(url, {})  # socket host: TLS is the proxy's job


def test_require_tls_parses_without_a_driver() -> None:
    require_tls("postgresql://u:p@[::1]:5432/db", {})
    require_tls("postgresql://u:p@localhost,127.0.0.1/db", {})
    require_tls("host=/var/run/postgresql dbname=db", {})
    require_tls("host=db.example.com sslmode=verify-full", {})
    require_tls("postgresql:///db", {"PGHOST": "localhost"})
    for bad in ("postgresql://u:p@h:notaport/db", "host"):
        with pytest.raises(ConfigError, match="malformed"):
            require_tls(bad, {})
    with pytest.raises(ConfigError, match="sslmode"):
        require_tls("host='db.example.com' sslmode=prefer", {})


def test_config_has_no_neon_transport() -> None:
    from claude_duel.config import load_settings  # noqa: PLC0415

    settings = load_settings(None, {"DUEL_RESULTS_BACKEND": "neon-http"})
    assert not hasattr(settings, "results_backend")
