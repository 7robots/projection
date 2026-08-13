"""Tests for the D1 HTTP client's retry and error behaviour.

The interesting difference from Smartsheet: every call is a POST, so the *method*
cannot say whether a replay is safe. The statement does, and the caller states it.
"""

import httpx
import pytest

from projection.d1_api import D1Client, D1Error

ACCOUNT = "acct-1"
DATABASE = "db-1"


def _client(handler) -> D1Client:
    """A client wired to an httpx MockTransport, with retries made instant."""
    client = D1Client(ACCOUNT, token="test-token")
    client._client = httpx.AsyncClient(
        base_url="https://api.cloudflare.test/client/v4",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-token"},
    )
    return client


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("projection.d1_api._RETRY_BASE_DELAY", 0)


def _ok(rows=None, meta=None):
    return httpx.Response(
        200,
        json={
            "success": True,
            "errors": [],
            "result": [{"results": rows or [], "meta": meta or {}, "success": True}],
        },
    )


class Counter:
    def __init__(self, responder):
        self.calls = 0
        self.requests = []
        self._responder = responder

    def __call__(self, request):
        self.calls += 1
        self.requests.append(request)
        return self._responder(self.calls, request)


# ==================== Retry safety is per statement ====================


async def test_an_unsafe_statement_is_not_retried_on_a_network_error():
    """A compare-and-swap replayed would report a conflict that never happened."""

    def boom(n, request):
        raise httpx.ReadTimeout("connection lost", request=request)

    counter = Counter(boom)
    with pytest.raises(D1Error, match="cannot be safely retried"):
        await _client(counter).query(DATABASE, "UPDATE x SET y = 1", retry_safe=False)
    assert counter.calls == 1


async def test_a_safe_statement_is_retried_on_a_network_error():
    def flaky(n, request):
        if n == 1:
            raise httpx.ReadTimeout("connection lost", request=request)
        return _ok([{"id": "1"}])

    counter = Counter(flaky)
    result = await _client(counter).query(DATABASE, "SELECT 1", retry_safe=True)
    assert counter.calls == 2
    assert result.rows == [{"id": "1"}]


async def test_an_unsafe_statement_is_not_retried_on_a_server_error():
    counter = Counter(lambda n, r: httpx.Response(503, json={"success": False}))
    with pytest.raises(D1Error):
        await _client(counter).query(DATABASE, "UPDATE x SET y = 1", retry_safe=False)
    assert counter.calls == 1


async def test_a_rate_limit_is_replayed_even_when_unsafe():
    """429 is rejected before the statement runs, so nothing was applied."""

    def limited(n, request):
        return httpx.Response(429) if n == 1 else _ok()

    counter = Counter(limited)
    await _client(counter).query(DATABASE, "UPDATE x SET y = 1", retry_safe=False)
    assert counter.calls == 2


async def test_creating_a_database_is_never_replayed():
    """Two databases, and config.toml naming whichever answered second."""

    def boom(n, request):
        raise httpx.ConnectError("dropped", request=request)

    counter = Counter(boom)
    with pytest.raises(D1Error, match="cannot be safely retried"):
        await _client(counter).create_database("projection")
    assert counter.calls == 1


# ==================== Errors are readable ====================


async def test_an_invalid_token_says_so():
    counter = Counter(
        lambda n, r: httpx.Response(
            401, json={"success": False, "errors": [{"code": 10000, "message": "bad"}]}
        )
    )
    with pytest.raises(D1Error, match="rejected the API token"):
        await _client(counter).query(DATABASE, "SELECT 1", retry_safe=True)


async def test_a_permission_error_names_the_permission_needed():
    counter = Counter(
        lambda n, r: httpx.Response(403, json={"success": False, "errors": []})
    )
    with pytest.raises(D1Error, match="D1 edit permission"):
        await _client(counter).query(DATABASE, "SELECT 1", retry_safe=True)


async def test_a_failure_reported_only_in_the_body_still_raises():
    """Cloudflare can answer 200 with success: false."""
    counter = Counter(
        lambda n, r: httpx.Response(
            200,
            json={
                "success": False,
                "errors": [{"code": 7500, "message": "no such table: projects"}],
            },
        )
    )
    with pytest.raises(D1Error, match="no such table"):
        await _client(counter).query(DATABASE, "SELECT 1", retry_safe=True)


async def test_a_missing_result_is_an_error_not_an_empty_read():
    """An empty read would be merged as "everything was deleted there"."""
    counter = Counter(lambda n, r: httpx.Response(200, json={"success": True}))
    with pytest.raises(D1Error, match="no query result"):
        await _client(counter).query(DATABASE, "SELECT 1", retry_safe=True)


# ==================== Request shape ====================


async def test_the_statement_and_params_are_sent_as_json():
    counter = Counter(lambda n, r: _ok())
    await _client(counter).query(
        DATABASE, "SELECT * FROM projects WHERE id = ?", ["abc"], retry_safe=True
    )
    request = counter.requests[0]
    assert request.url.path.endswith(f"/d1/database/{DATABASE}/query")
    import json as _json

    body = _json.loads(request.content)
    assert body == {"sql": "SELECT * FROM projects WHERE id = ?", "params": ["abc"]}


async def test_changes_is_none_when_d1_does_not_report_it():
    """None is not zero — a CAS reading it as zero would invent a conflict."""
    counter = Counter(lambda n, r: _ok(meta={}))
    result = await _client(counter).query(DATABASE, "SELECT 1", retry_safe=True)
    assert result.changes is None

    counter = Counter(lambda n, r: _ok(meta={"changes": 0}))
    result = await _client(counter).query(DATABASE, "SELECT 1", retry_safe=True)
    assert result.changes == 0
