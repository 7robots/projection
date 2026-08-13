"""Tests for the Smartsheet REST client's retry and error behaviour."""

import httpx
import pytest

from projection.smartsheet_api import (
    SmartsheetClient,
    SmartsheetError,
    columns_by_title,
)


def _client(handler) -> SmartsheetClient:
    """A client wired to an httpx MockTransport, with retries made instant."""
    client = SmartsheetClient(token="test-token")
    client._client = httpx.AsyncClient(
        base_url="https://api.smartsheet.test/2.0",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-token"},
    )
    return client


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("projection.smartsheet_api._RETRY_BASE_DELAY", 0)


class Counter:
    def __init__(self, responder):
        self.calls = 0
        self._responder = responder

    def __call__(self, request):
        self.calls += 1
        return self._responder(self.calls, request)


# ---- retry safety -------------------------------------------------------


async def test_post_is_not_retried_on_server_error():
    """A create Smartsheet may have applied must never be replayed."""
    counter = Counter(lambda n, r: httpx.Response(503))
    with pytest.raises(SmartsheetError):
        await _client(counter).add_rows(1, [{"cells": []}])
    assert counter.calls == 1


async def test_post_is_not_retried_on_network_error():
    def boom(n, request):
        raise httpx.ReadTimeout("connection lost", request=request)

    counter = Counter(boom)
    with pytest.raises(SmartsheetError, match="cannot be safely retried"):
        await _client(counter).add_rows(1, [{"cells": []}])
    assert counter.calls == 1


async def test_post_is_retried_on_rate_limit():
    """429 is rejected before processing, so replaying it can't duplicate."""
    counter = Counter(
        lambda n, r: httpx.Response(429)
        if n == 1
        else httpx.Response(200, json={"result": [{"id": 5}]})
    )
    rows = await _client(counter).add_rows(1, [{"cells": []}])
    assert counter.calls == 2
    assert rows == [{"id": 5}]


async def test_put_is_retried_on_server_error():
    """Updates are idempotent, so replaying them is safe and useful."""
    counter = Counter(
        lambda n, r: httpx.Response(502)
        if n < 3
        else httpx.Response(200, json={"result": [{"id": 7}]})
    )
    rows = await _client(counter).update_rows(1, [{"id": 7, "cells": []}])
    assert counter.calls == 3
    assert rows == [{"id": 7}]


async def test_retries_are_bounded():
    counter = Counter(lambda n, r: httpx.Response(503))
    with pytest.raises(SmartsheetError, match="after 4 tries"):
        await _client(counter).get_sheet(1)
    assert counter.calls == 4


# ---- error surfacing ----------------------------------------------------


async def test_401_is_explained():
    counter = Counter(lambda n, r: httpx.Response(401, json={"message": "nope"}))
    with pytest.raises(SmartsheetError, match="rejected the API token"):
        await _client(counter).get_sheet(1)


async def test_api_message_is_surfaced():
    counter = Counter(
        lambda n, r: httpx.Response(
            400, json={"message": "Invalid column value", "errorCode": 1042}
        )
    )
    with pytest.raises(SmartsheetError, match="Invalid column value"):
        await _client(counter).get_sheet(1)


async def test_errors_never_carry_the_token():
    """Error text is user-facing; the bearer token must never reach it."""
    def boom(n, request):
        raise httpx.ConnectError("failed", request=request)

    counter = Counter(boom)
    with pytest.raises(SmartsheetError) as excinfo:
        await _client(counter).get_sheet(1)
    assert "test-token" not in str(excinfo.value)


async def test_delete_sends_ids_as_query_params():
    seen = {}

    def handler(n, request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"result": []})

    await _client(Counter(handler)).delete_rows(1, [11, 22])
    assert "ids=11%2C22" in seen["url"] or "ids=11,22" in seen["url"]


async def test_empty_row_lists_skip_the_network():
    counter = Counter(lambda n, r: httpx.Response(500))
    client = _client(counter)
    assert await client.add_rows(1, []) == []
    assert await client.update_rows(1, []) == []
    assert await client.delete_rows(1, []) is None
    assert counter.calls == 0


# ---- column resolution --------------------------------------------------


def test_columns_by_title_maps_required_columns():
    sheet = {"name": "S", "columns": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]}
    assert columns_by_title(sheet, ("A", "B")) == {"A": 1, "B": 2}


def test_columns_by_title_rejects_missing():
    sheet = {"name": "S", "columns": [{"id": 1, "title": "A"}]}
    with pytest.raises(SmartsheetError, match="'B'"):
        columns_by_title(sheet, ("A", "B"))


def test_columns_by_title_rejects_duplicates():
    """Silently keeping the last would send writes to the wrong column."""
    sheet = {
        "name": "S",
        "columns": [{"id": 1, "title": "A"}, {"id": 2, "title": "A"}],
    }
    with pytest.raises(SmartsheetError, match="more than one column titled"):
        columns_by_title(sheet, ("A",))


def test_columns_by_title_ignores_duplicates_it_does_not_use():
    sheet = {
        "name": "S",
        "columns": [
            {"id": 1, "title": "A"},
            {"id": 2, "title": "Unused"},
            {"id": 3, "title": "Unused"},
        ],
    }
    assert columns_by_title(sheet, ("A",))["A"] == 1
