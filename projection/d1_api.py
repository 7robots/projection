"""Thin async client for Cloudflare's D1 HTTP API.

Projection talks to D1 directly over HTTPS with an account API token — no Worker
in front of it. A Worker would mean running a service, owning its auth, and a
deploy step in the edit loop; the only thing it would buy is sharing the data
with people who should not hold a D1 token, which is not the case this backend is
for.

**Retry safety works differently here than it does for Smartsheet.** There, the
HTTP method says whether a replay is safe. Here *every* call is `POST /query`, so
the method says nothing and the **statement** decides:

| Statement | Safe to replay? |
|---|---|
| `SELECT` | yes |
| `INSERT … ON CONFLICT DO UPDATE` (id supplied by us) | yes — an upsert on a known primary key is idempotent |
| `UPDATE … SET x = ?` (no version check) | yes — writing the same values twice is the same as once |
| `UPDATE … WHERE updated_at = ?` (compare-and-swap) | **no** — a replay after the first succeeded finds the stamp changed and reports a false conflict |
| `DELETE … WHERE id = ?` | yes |
| `CREATE TABLE IF NOT EXISTS` | yes |

So `query()` takes `retry_safe` explicitly and callers state it per statement.
Defaulting it to True would make the one dangerous case the silent default.
"""

from typing import Any, Optional
import asyncio

import httpx

from .secrets import Credential, load_token

BASE_URL = "https://api.cloudflare.com/client/v4"

_RATE_LIMIT_STATUS = 429
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})

_MAX_TRIES = 4
_RETRY_BASE_DELAY = 1.5

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class D1Error(Exception):
    """A D1 API call failed."""


class D1Client:
    """Async client for one Cloudflare account's D1 databases.

    The token is loaded lazily on first use, so constructing this (in a test, or
    when a panel mounts) never shells out to 1Password.
    """

    def __init__(
        self,
        account_id: str,
        *,
        token: Optional[str] = None,
        credential: Optional[Credential] = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._account_id = account_id
        self._token = token
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None:
                if self._token is None:
                    if self._credential is None:
                        raise D1Error(
                            "No Cloudflare credential configured for this client."
                        )
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
        """Load the token and open the session, surfacing auth errors now."""
        await self._ensure_client()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ==================== Transport ====================

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        retry_safe: bool,
    ) -> dict:
        client = await self._ensure_client()
        last_detail = ""

        for attempt in range(_MAX_TRIES):
            if attempt:
                await asyncio.sleep(_RETRY_BASE_DELAY * attempt)
            try:
                response = await client.request(method, path, json=json)
            except httpx.HTTPError as e:
                last_detail = f"network error: {e}"
                if not retry_safe:
                    raise D1Error(
                        f"{method} {path} failed and cannot be safely retried "
                        f"({last_detail}). Refresh to check whether it applied."
                    )
                continue

            # A rate limit is rejected before the statement runs, so replaying it
            # is safe even for a write.
            if response.status_code == _RATE_LIMIT_STATUS or (
                response.status_code in _TRANSIENT_STATUSES and retry_safe
            ):
                last_detail = f"HTTP {response.status_code}"
                continue

            try:
                body = response.json()
            except ValueError as e:
                if response.status_code >= 400:
                    raise D1Error(f"Cloudflare HTTP {response.status_code}")
                raise D1Error(f"Cloudflare returned invalid JSON: {e}")

            if response.status_code >= 400 or not _succeeded(body):
                raise D1Error(_error_message(response.status_code, body))
            return body if isinstance(body, dict) else {}

        raise D1Error(f"{method} {path} failed after {_MAX_TRIES} tries ({last_detail})")

    # ==================== Databases ====================

    async def create_database(self, name: str) -> dict:
        """Create a database in this account, returning its record."""
        body = await self._request(
            "POST",
            f"/accounts/{self._account_id}/d1/database",
            json={"name": name},
            # Not replayable: a lost response would leave one database created
            # and a second attempt naming a different one in config.toml.
            retry_safe=False,
        )
        created = body.get("result")
        if not isinstance(created, dict) or not created.get("uuid"):
            raise D1Error("Cloudflare did not return the created database")
        return created

    async def get_database(self, database_id: str) -> dict:
        """One database's record — used to check it exists and read its name."""
        body = await self._request(
            "GET",
            f"/accounts/{self._account_id}/d1/database/{database_id}",
            retry_safe=True,
        )
        found = body.get("result")
        if not isinstance(found, dict):
            raise D1Error("Cloudflare returned no database record")
        return found

    # ==================== Queries ====================

    async def query(
        self,
        database_id: str,
        sql: str,
        params: Optional[list] = None,
        *,
        retry_safe: bool,
    ) -> "QueryResult":
        """Run one statement. `retry_safe` states whether a replay is harmless.

        See the module docstring: the HTTP method cannot answer that here, so
        every caller has to.
        """
        body = await self._request(
            "POST",
            f"/accounts/{self._account_id}/d1/database/{database_id}/query",
            json={"sql": sql, "params": list(params or [])},
            retry_safe=retry_safe,
        )
        results = body.get("result")
        if not isinstance(results, list) or not results:
            raise D1Error("Cloudflare returned no query result")
        first = results[0] if isinstance(results[0], dict) else {}
        rows = first.get("results")
        meta = first.get("meta")
        return QueryResult(
            rows=[r for r in (rows or []) if isinstance(r, dict)],
            meta=meta if isinstance(meta, dict) else {},
        )


class QueryResult:
    """One statement's rows, plus D1's own metadata about it."""

    def __init__(self, rows: list[dict], meta: dict) -> None:
        self.rows = rows
        self.meta = meta

    @property
    def changes(self) -> Optional[int]:
        """How many rows the statement changed, or None if D1 didn't say.

        None is not zero. A compare-and-swap that reads this as "no rows matched"
        when the answer was merely absent would report a conflict that never
        happened, so callers must treat None as unknown.
        """
        for key in ("changes", "rows_written"):
            value = self.meta.get(key)
            if isinstance(value, int):
                return value
        return None


def _succeeded(body: Any) -> bool:
    """Cloudflare reports failure in the body as well as the status code."""
    if not isinstance(body, dict):
        return False
    return body.get("success") is not False


def _error_message(status: int, body: Any) -> str:
    """A readable error from a Cloudflare error response."""
    errors = body.get("errors") if isinstance(body, dict) else None
    detail = ""
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            code = first.get("code")
            message = first.get("message") or ""
            detail = f"{message} (code {code})" if code else str(message)
    if status == 401:
        return "Cloudflare rejected the API token (401) — is it valid and unexpired?"
    if status == 403:
        return (
            "Cloudflare denied access (403): "
            f"{detail or 'the token needs D1 edit permission on this account'}"
        )
    if status == 404:
        return f"Cloudflare could not find it (404): {detail or 'check the ids'}"
    if detail:
        return f"Cloudflare error: {detail}"
    return f"Cloudflare HTTP {status}"
