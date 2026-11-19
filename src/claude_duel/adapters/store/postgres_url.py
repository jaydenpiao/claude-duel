"""Connection-string checks for the results database, with no driver import."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

from claude_duel.domain.errors import ConfigError

_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})
_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})


def iam_url(url: str) -> str:
    """Percent-encode the ``@`` inside a Cloud SQL IAM user, so libpq reads the host after the last ``@``.

    ``postgresql://duel-sql-proxy@<project>.iam@/duel?host=/run/duel-proxy/<conn>`` (cloud-sql-proxy
    ``--auto-iam-authn`` on a Unix socket, no password) becomes ``postgresql://duel-sql-proxy%40<project>.iam@/duel?...``.
    Other URLs pass through.
    """
    if not url.startswith(("postgres://", "postgresql://")):
        return url
    scheme, _, rest = url.partition("://")
    netloc, slash, tail = rest.partition("/")
    userinfo, at, host = netloc.rpartition("@")
    if not at or "@" not in userinfo:
        return url
    user, colon, password = userinfo.partition(":")
    return f"{scheme}://{quote(user, safe='%')}{colon}{password}@{host}{slash}{tail}"


def _params(url: str) -> dict[str, str]:
    if url.startswith(("postgres://", "postgresql://")):
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a malformed port
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        netloc = parts.netloc.rpartition("@")[2]
        hosts = ",".join(
            unquote(h.rsplit(":", 1)[0] if not h.startswith("[") else h[1 : h.index("]")]) for h in netloc.split(",")
        )
        return {**({"host": hosts} if hosts else {}), **query}
    out = {}
    for item in shlex.split(url):
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(key)
        out[key.strip()] = value
    return out


def _user_password(url: str, info: Mapping[str, str], env: Mapping[str, str]) -> tuple[str, str]:
    user = password = ""
    if url.startswith(("postgres://", "postgresql://")):
        userinfo = urlsplit(url).netloc.rpartition("@")[0]
        user, _, password = userinfo.partition(":")
    user = info.get("user") or unquote(user) or env.get("PGUSER", "")
    return user, info.get("password") or password or env.get("PGPASSWORD", "")


def _refuse_service(info: Mapping[str, str], env: Mapping[str, str]) -> None:
    """pg_service.conf could supply a host and sslmode this check never sees."""
    if info.get("service") or env.get("PGSERVICE") or env.get("PGSERVICEFILE"):
        raise ConfigError("the results database URL may not use a libpq service (service, PGSERVICE, PGSERVICEFILE)")


def _hosts(info: Mapping[str, str], env: Mapping[str, str]) -> list[str]:
    """Addresses libpq will dial: ``hostaddr``/PGHOSTADDR wins over ``host``/PGHOST."""
    addr = info.get("hostaddr") or env.get("PGHOSTADDR", "")
    return (addr or info.get("host") or env.get("PGHOST", "")).split(",")


def _remote_hosts(hosts: list[str]) -> list[str]:
    """Non-loopback, non-socket hosts that need TLS."""
    return [h for h in hosts if h.strip() not in _LOCAL_HOSTS and not h.startswith("/")]


def _refuse_iam_tcp(user: str, password: str, hosts: list[str]) -> None:
    """A passwordless IAM user is only safe through the proxy's 0700 Unix socket, never TCP."""
    if user.endswith(".iam") and not password and not all(h.startswith("/") for h in hosts):
        raise ConfigError("a Cloud SQL IAM user without a password must connect through the proxy's Unix socket")


def require_tls(url: str, env: Mapping[str, str] | None = None) -> None:
    """Refuse a non-local host unless ``sslmode`` is require or stricter (libpq's default ``prefer`` downgrades).

    Loopback hosts and unix sockets (``/path`` hosts) are exempt. ``hostaddr``/PGHOSTADDR, when set, is the address
    libpq dials, so it is checked instead of ``host``. ``service``, PGSERVICE and PGSERVICEFILE are refused because
    pg_service.conf could supply a host and sslmode this check never sees.
    A passwordless ``*.iam`` user is refused unless every host is a socket path.
    Neither the URL nor a parser message is echoed.
    """
    env = os.environ if env is None else env
    try:
        info = _params(url)
    except ValueError:
        raise ConfigError("the results database URL is malformed") from None
    _refuse_service(info, env)
    hosts = _hosts(info, env)
    user, password = _user_password(url, info, env)
    _refuse_iam_tcp(user, password, hosts)
    mode = info.get("sslmode") or env.get("PGSSLMODE", "prefer")
    if _remote_hosts(hosts) and mode not in _TLS_MODES:
        raise ConfigError("a remote results database needs sslmode=require, verify-ca or verify-full in its URL")
