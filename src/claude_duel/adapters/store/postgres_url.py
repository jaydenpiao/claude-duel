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

    ``postgresql://duel-sql-proxy@<project>.iam@127.0.0.1:5433/duel`` (cloud-sql-proxy ``--auto-iam-authn``, no
    password) becomes ``postgresql://duel-sql-proxy%40<project>.iam@127.0.0.1:5433/duel``. Other URLs pass through.
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


def require_tls(url: str, env: Mapping[str, str] | None = None) -> None:
    """Refuse a non-local host unless ``sslmode`` is require or stricter (libpq's default ``prefer`` downgrades).

    Loopback hosts and unix sockets (``/path`` hosts) are exempt. Neither the URL nor a parser message is echoed.
    """
    env = os.environ if env is None else env
    try:
        info = _params(url)
    except ValueError:
        raise ConfigError("the results database URL is malformed") from None
    hosts = (info.get("host") or env.get("PGHOST", "")).split(",")
    remote = [h for h in hosts if h.strip() not in _LOCAL_HOSTS and not h.startswith("/")]
    mode = info.get("sslmode") or env.get("PGSSLMODE", "prefer")
    if remote and mode not in _TLS_MODES:
        raise ConfigError("a remote results database needs sslmode=require, verify-ca or verify-full in its URL")
