"""Thin async client for the Smartsheet REST API v2.

Projection talks to Smartsheet directly over HTTPS with a personal access
token, so there is no OAuth dance and no MCP server in the data path. The
token comes from 1Password at launch (see `secrets.py`).
"""

import asyncio
from typing import Any, Optional

import httpx

from .secrets import SMARTSHEET as SMARTSHEET_CREDENTIAL, Credential, load_token

BASE_URL = "https://api.smartsheet.com/2.0"

# Smartsheet rejects rate-limited requests before processing them, so a 429 is
# always safe to replay — even for a POST.
_RATE_LIMIT_STATUS = 429
# 5xx and dropped connections are only safe to replay for idempotent methods:
# the server may well have applied the change and lost the response.
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
_IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE"})

_MAX_TRIES = 4
_RETRY_BASE_DELAY = 1.5

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class SmartsheetError(Exception):
    """A Smartsheet API call failed."""


class SmartsheetClient:
    """Async Smartsheet API client.

    The token is loaded lazily on first use so importing this module (and
    constructing the client in tests) never shells out to 1Password.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        credential: Optional[Credential] = None,
        base_url: str = BASE_URL,
    ) -> None:
        """`credential` says where to read the token — its `op://` reference is
        configurable per backend, so it comes from config rather than from here.
        """
        self._token = token
        self._credential = credential or SMARTSHEET_CREDENTIAL
        self._base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None:
                if self._token is None:
                    # Blocking subprocess (`op read`) — keep it off the event loop.
                    self._token = await asyncio.to_thread(
                        load_token, self._credential
                    )
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=_TIMEOUT,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                )
            return self._client

    async def ensure_ready(self) -> None:
        """Load the token and open the HTTP session, surfacing auth errors now."""
        await self._ensure_client()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Any = None,
    ) -> Any:
        client = await self._ensure_client()
        # A non-idempotent call (POST creates a row) must not be replayed after
        # a lost response — that is how one project becomes two rows.
        replayable = method.upper() in _IDEMPOTENT_METHODS
        last_detail = ""

        for attempt in range(_MAX_TRIES):
            if attempt:
                await asyncio.sleep(_RETRY_BASE_DELAY * attempt)
            try:
                response = await client.request(
                    method, path, params=params, json=json
                )
            except httpx.HTTPError as e:
                last_detail = f"network error: {e}"
                if not replayable:
                    raise SmartsheetError(
                        f"{method} {path} failed and cannot be safely retried "
                        f"({last_detail}). Refresh to check whether it applied."
                    )
                continue

            if response.status_code == _RATE_LIMIT_STATUS or (
                response.status_code in _TRANSIENT_STATUSES and replayable
            ):
                last_detail = f"HTTP {response.status_code}"
                continue

            if response.status_code >= 400:
                raise SmartsheetError(_error_message(response))

            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as e:
                raise SmartsheetError(f"Smartsheet returned invalid JSON: {e}")

        raise SmartsheetError(
            f"{method} {path} failed after {_MAX_TRIES} tries ({last_detail})"
        )

    # ==================== Sheet operations ====================

    async def get_sheet(self, sheet_id: int, **params: Any) -> dict:
        """Fetch a sheet with its columns and rows.

        `level=2` plus `include=objectValue` is what makes MULTI_CONTACT_LIST
        cells come back as structured contacts rather than a joined string.
        """
        query = {"level": 2, "include": "objectValue"}
        query.update(params)
        return await self._request("GET", f"/sheets/{sheet_id}", params=query)

    async def create_sheet(self, name: str, columns: list[dict]) -> dict:
        """Create a sheet in the user's Sheets home, returning the created sheet.

        A POST, so `_request` refuses to replay it: a create replayed after a
        lost response is how one sheet becomes two, and a duplicate *sheet* is
        worse than a duplicate row — the id in config.toml would name whichever
        one the second attempt returned.
        """
        data = await self._request(
            "POST", "/sheets", json={"name": name, "columns": columns}
        )
        created = data.get("result") if isinstance(data, dict) else None
        if not isinstance(created, dict) or not created.get("id"):
            raise SmartsheetError("Smartsheet did not return the created sheet")
        return created

    async def add_rows(self, sheet_id: int, rows: list[dict]) -> list[dict]:
        """Add rows; returns the created rows (with their new ids)."""
        if not rows:
            return []
        data = await self._request("POST", f"/sheets/{sheet_id}/rows", json=rows)
        return _result_rows(data)

    async def update_rows(self, sheet_id: int, rows: list[dict]) -> list[dict]:
        """Update rows in place; each row dict needs an `id`."""
        if not rows:
            return []
        data = await self._request("PUT", f"/sheets/{sheet_id}/rows", json=rows)
        return _result_rows(data)

    async def delete_rows(self, sheet_id: int, row_ids: list[int]) -> None:
        """Delete rows by id."""
        if not row_ids:
            return
        await self._request(
            "DELETE",
            f"/sheets/{sheet_id}/rows",
            params={"ids": ",".join(str(r) for r in row_ids)},
        )


def columns_by_title(sheet: dict, required: tuple[str, ...]) -> dict[str, int]:
    """Map column title -> id, failing loudly on missing or duplicated titles.

    A plain dict comprehension would silently keep the last of two columns
    sharing a title, which would send a write to the wrong column.
    """
    seen: dict[str, int] = {}
    duplicated: set[str] = set()
    for col in sheet.get("columns", []):
        title, column_id = col.get("title"), col.get("id")
        if not title or not column_id:
            continue
        if title in seen:
            duplicated.add(title)
        seen[title] = column_id

    sheet_name = sheet.get("name") or "sheet"
    missing = [name for name in required if name not in seen]
    if missing:
        raise SmartsheetError(
            f"{sheet_name} has no {', '.join(repr(m) for m in missing)} "
            "column — has the sheet layout changed?"
        )
    ambiguous = sorted(duplicated & set(required))
    if ambiguous:
        raise SmartsheetError(
            f"{sheet_name} has more than one column titled "
            f"{', '.join(repr(a) for a in ambiguous)} — rename one so writes "
            "are unambiguous."
        )
    return seen


def _error_message(response: httpx.Response) -> str:
    """Build a readable error from a Smartsheet error response."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = body.get("message") if isinstance(body, dict) else None
    code = body.get("errorCode") if isinstance(body, dict) else None
    if response.status_code == 401:
        return "Smartsheet rejected the API token (401) — is it valid and unexpired?"
    if response.status_code == 403:
        return f"Smartsheet denied access (403): {message or 'check sheet permissions'}"
    if message:
        return f"Smartsheet error {code or response.status_code}: {message}"
    return f"Smartsheet HTTP {response.status_code}"


def _result_rows(data: Any) -> list[dict]:
    """Pull the row list out of a write response."""
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    return []
