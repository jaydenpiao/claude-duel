"""neon_http request/response mapping without a network, plus the shared SQL helpers (results_sql, postgres_url)."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from claude_duel.adapters.store.neon_http import NeonHttpExecutor, check_endpoint, endpoint_for
from claude_duel.adapters.store.postgres_url import require_tls
from claude_duel.adapters.store.results_sql import split_statements
from claude_duel.app.container import results_executor
from claude_duel.config import load_settings
from claude_duel.domain.errors import ConfigError, ResultsDbError

URL = "postgresql://u:s3cret-pw@ep-x-pooler.c-2.us-east-2.aws.neon.tech/db?sslmode=require"


def _client(handler: object, seen: list[httpx.Request]) -> httpx.Client:
    def wrap(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)  # type: ignore[operator]

    return httpx.Client(transport=httpx.MockTransport(wrap))


def test_endpoint_derivation_and_scheme() -> None:
    assert endpoint_for(URL) == "https://api.c-2.us-east-2.aws.neon.tech/sql"
    with pytest.raises(ConfigError, match="Neon host"):
        endpoint_for("postgresql://u:p@localhost/db")
    assert check_endpoint("http://127.0.0.1:4444/sql")
    assert check_endpoint("http://db.localtest.me:4444/sql")
    with pytest.raises(ConfigError, match="https"):
        check_endpoint("http://api.example.com/sql")


def test_query_maps_params_and_types() -> None:
    seen: list[httpx.Request] = []
    fields = [{"name": n, "dataTypeID": t} for n, t in (("b", 16), ("i", 20), ("n", 1700), ("j", 3802), ("t", 25))]
    body = {"fields": fields, "rows": [["t", "7", "1.5", '{"a": 1}', "x"], ["f", None, "0", "[]", "y"]]}
    ex = NeonHttpExecutor(URL, client=_client(lambda _: httpx.Response(200, json=body), seen))
    got = ex.query("select %s, %s, %s, %s, %s", [True, None, 3, {"k": 1}, "s"])
    assert got == [
        {"b": True, "i": 7, "n": Decimal("1.5"), "j": {"a": 1}, "t": "x"},
        {"b": False, "i": None, "n": Decimal(0), "j": [], "t": "y"},
    ]
    req = seen[0]
    assert str(req.url) == "https://api.c-2.us-east-2.aws.neon.tech/sql"
    assert req.headers["Neon-Connection-String"] == URL
    assert json.loads(req.content) == {
        "query": "select $1, $2, $3, $4, $5",
        "params": ["true", None, "3", '{"k": 1}', "s"],
    }
    ex.query("select '100%s'")  # no params: the text is sent untouched
    assert json.loads(seen[1].content)["query"] == "select '100%s'"


def test_batch_is_one_request() -> None:
    seen: list[httpx.Request] = []
    resp = {"results": [{"rowCount": 2}, {"rowCount": None}]}
    ex = NeonHttpExecutor(URL, client=_client(lambda _: httpx.Response(200, json=resp), seen))
    assert ex.batch([("insert %s", [False]), ("select 1", [])]) == [2, 0]
    sent = json.loads(seen[0].content)
    assert sent["queries"] == [{"query": "insert $1", "params": ["false"]}, {"query": "select 1", "params": []}]
    with pytest.raises(ConfigError, match="--rebuild"), ex.transaction():
        pass


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(400, json={"message": "bad s3cret-pw value", "code": "22P02"}), "HTTP 400 (22P02)"),
        (httpx.Response(401, json={"message": "auth failed for s3cret-pw", "code": ""}), "HTTP 401 (no sqlstate)"),
        (httpx.Response(502, text="<html>s3cret-pw</html>"), "HTTP 502 (no sqlstate)"),
    ],
)
def test_errors_never_echo(response: httpx.Response, expected: str) -> None:
    ex = NeonHttpExecutor(URL, client=_client(lambda _: response, []))
    with pytest.raises(ResultsDbError) as err:
        ex.query("select 1")
    assert expected in str(err.value)
    assert "s3cret" not in str(err.value)


def test_network_errors_never_echo() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {req.headers['Neon-Connection-String']}")

    ex = NeonHttpExecutor(URL, client=_client(boom, []))
    with pytest.raises(ResultsDbError, match=r"unreachable \(ConnectError\)") as err:
        ex.query("select 1")
    assert "s3cret" not in str(err.value)


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


def test_split_statements() -> None:
    sql = """
    -- a comment; with a semicolon
    create table t (x text default 'a;b' , "we;ird" int);
    do $body$ begin perform 1; end $body$;
    select $$x;y$$, 'it''s;', $1::int;
    """
    assert split_statements(sql) == [
        """create table t (x text default 'a;b' , "we;ird" int)""",
        "do $body$ begin perform 1; end $body$",
        "select $$x;y$$, 'it''s;', $1::int",
    ]


def test_container_picks_the_transport() -> None:
    env = {"DUEL_RESULTS_BACKEND": "neon-http", "DUEL_NEON_HTTP_ENDPOINT": "http://127.0.0.1:4444/sql"}
    settings = load_settings(None, env)
    assert isinstance(results_executor(settings, URL), NeonHttpExecutor)  # no request until the first query
    with pytest.raises(ConfigError, match="sslmode"):
        results_executor(settings, "postgresql://u:p@ep-x.neon.tech/db")
    with pytest.raises(ConfigError, match="results_backend"):
        load_settings(None, {"DUEL_RESULTS_BACKEND": "mysql"})
