"""The Cloudflare D1 backend.

The second implementation of `Backend`, and the one that tests whether the
interface is real rather than a description of Smartsheet. Three ways it differs,
each of which the protocol had to accommodate:

- **Identity is shared.** The table's primary key *is* `Project.id`, so the same
  project has the same id on every device syncing through this database. A
  Smartsheet row id is a key Projection has to map; here there is nothing to map,
  which is why `fetch()` sets `Project.id` from the row rather than leaving it
  provisional.
- **Compare-and-swap is real.** `UPDATE … WHERE id = ? AND updated_at = ?` either
  applies or reports that someone else got there first (`StaleRecordError`).
  Smartsheet has no equivalent, so a concurrent write there is only ever caught
  afterwards, by the merge.
- **Structure is repairable.** A database with no table in it is something this
  backend can fix itself, which a spreadsheet missing columns is not.

There is no column mapping. `FieldMap` exists because a sheet arrives with a
vocabulary somebody else chose; a table Projection creates has the canonical
names, and adopting a *different* schema is not a case this backend takes on —
`table` names the table, and its columns are ours.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..d1_api import D1Client, D1Error
from ..secrets import Credential
from ..models import (
    FIELD_NAMES,
    Person,
    Project,
    ProjectFields,
    people,
    to_iso_date,
)
from .base import (
    Backend,
    BackendError,
    Capabilities,
    ProbeResult,
    ProvisionResult,
    RecordRef,
    StaleRecordError,
)

NAME = "d1"

DEFAULT_TABLE = "projects"
DEFAULT_DATABASE_NAME = "projection"

# Read in one statement, so a truncated read cannot pass for a complete one.
# `fetch()` must return everything or raise (see the protocol), and the HTTP API
# has no cursor — so the guard is an explicit ceiling rather than silent
# pagination that stops early.
MAX_ROWS = 5000

# The columns are ours, in canonical order. `updated_at` is the backend's own
# modification time, written server-side.
_COLUMNS = ("id",) + FIELD_NAMES + ("updated_at",)

_SELECT_COLUMNS = ", ".join(_COLUMNS)

# `strftime` rather than a client timestamp: with two devices writing, the
# ordering has to come from one clock, and it should not be either laptop's.
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def _quote_ident(name: str) -> str:
    """A table name as a safe SQL identifier.

    Identifiers cannot be bound as parameters, so the table name — which comes
    from config.toml — is validated and quoted rather than interpolated raw.
    """
    if not name or not all(c.isalnum() or c == "_" for c in name):
        raise BackendError(
            f"{name!r} is not a usable table name — letters, digits and "
            "underscores only."
        )
    return f'"{name}"'


def _people_out(assigned: Any) -> str:
    """Assignees as stored JSON: a list of {name, email}."""
    return json.dumps(
        [{"name": p.name, "email": p.email} for p in people(assigned or [])]
    )


def _people_in(raw: Any) -> list[Person]:
    """Assignees back from stored JSON, tolerating a plain comma-joined string."""
    if not raw:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                return people(json.loads(text))
            except (ValueError, TypeError):
                return []
        # Not written by us — a hand-edited row. Read it as names.
        return people([part.strip() for part in text.split(",") if part.strip()])
    return people(raw if isinstance(raw, list) else [])


class D1Backend:
    """Read and write projects in a Cloudflare D1 table."""

    name = NAME
    capabilities = Capabilities(
        row_addressable=True,
        # A real version check, unlike Smartsheet: SQL can refuse the write.
        supports_cas=True,
        can_provision=True,
        # Assignees are stored as JSON text. A name with no email round-trips
        # fine here, which a Smartsheet contact column will not accept.
        typed_contacts=False,
        reports_modified_at=True,
    )

    def __init__(
        self,
        client: Optional[D1Client] = None,
        *,
        account_id: str = "",
        database_id: str = "",
        database_name: str = "",
        table: str = DEFAULT_TABLE,
        credential: Optional[Credential] = None,
    ) -> None:
        """A client may be handed in; whoever created it owns closing it.

        Without one, the backend builds its own from `account_id` and
        `credential` — and then closes it, which is why it is built here rather
        than by `build_backend`: a client the backend did not create is one
        nobody closes.
        """
        if client is None and not account_id:
            raise BackendError(
                "A Cloudflare account id is needed. Set account_id under "
                "[backends.d1] in config.toml, or run setup."
            )
        self._owns_client = client is None
        self._client = client or D1Client(account_id, credential=credential)
        self._account_id = account_id
        self._database_id = database_id
        self._database_name = database_name
        self._table = table or DEFAULT_TABLE
        # Validate now rather than at the first query, so a bad name in
        # config.toml is reported when the backend is built.
        self._quoted = _quote_ident(self._table)

    @property
    def database_id(self) -> str:
        """Which database this backend is pointed at ("" before provisioning)."""
        return self._database_id

    @property
    def database_name(self) -> str:
        return self._database_name

    @property
    def table(self) -> str:
        return self._table

    def _require_database(self) -> str:
        if not self._database_id:
            raise BackendError(
                "No D1 database is configured — run setup to create or connect "
                "one."
            )
        return self._database_id

    # ==================== Probing and provisioning ====================

    async def ensure_ready(self) -> None:
        """Load the Cloudflare token and open the session."""
        await self._client.ensure_ready()

    async def probe(self) -> ProbeResult:
        """Check the database exists and carries Projection's table."""
        if not self._database_id:
            return ProbeResult(
                ready=False,
                exists=False,
                detail="No database chosen yet — Projection can create one.",
            )
        try:
            record = await self._client.get_database(self._database_id)
        except D1Error as e:
            return ProbeResult(ready=False, exists=False, detail=str(e))

        name = str(record.get("name") or "")
        if self._database_name and name and name != self._database_name:
            return ProbeResult(
                ready=False,
                exists=True,
                detail=(
                    f"Database {self._database_id} is named {name!r}, expected "
                    f"{self._database_name!r} — check the configured id."
                ),
            )

        try:
            columns = await self._table_columns()
        except D1Error as e:
            return ProbeResult(ready=False, exists=True, detail=str(e))

        if not columns:
            # The database is there and the table is not. This backend owns that
            # table, so this is the one not-ready case it can simply fix.
            return ProbeResult(
                ready=False,
                exists=True,
                repairable=True,
                missing_fields=FIELD_NAMES,
                detail=(
                    f"{name or 'The database'} has no {self._table!r} table yet "
                    "— Projection can create it."
                ),
            )

        missing = tuple(f for f in FIELD_NAMES if f not in columns)
        if missing:
            # A table of this name exists but is not ours. Adding columns to
            # someone else's table is not setup's call.
            return ProbeResult(
                ready=False,
                exists=True,
                missing_fields=missing,
                detail=(
                    f"The {self._table!r} table is missing: "
                    + ", ".join(missing)
                    + ". Choose a different table name, or drop that table."
                ),
            )
        return ProbeResult(
            ready=True, exists=True, detail=f"{name or 'database'}.{self._table} ready"
        )

    async def _table_columns(self) -> set[str]:
        """The table's column names, or an empty set if it doesn't exist.

        The name is inlined rather than bound: an identifier cannot be a
        parameter, and `_quote_ident` has already restricted it to letters,
        digits and underscores.
        """
        result = await self._client.query(
            self._require_database(),
            f"SELECT name FROM pragma_table_info('{self._table}')",
            retry_safe=True,
        )
        return {str(row.get("name")) for row in result.rows if row.get("name")}

    async def provision(self) -> ProvisionResult:
        """Create the database if needed, and Projection's table inside it.

        Safe to call on a database that already exists — that is the *repair*
        case, where the database is there and the table is not. It is not
        destructive in either case: the table is created `IF NOT EXISTS` and no
        existing row is touched.
        """
        created_database = False
        if not self._database_id:
            name = self._database_name or DEFAULT_DATABASE_NAME
            record = await self._client.create_database(name)
            self._database_id = str(record["uuid"])
            self._database_name = str(record.get("name") or name)
            created_database = True

        await self._client.query(
            self._database_id,
            f"""
            CREATE TABLE IF NOT EXISTS {self._quoted} (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT '',
                status     TEXT,
                assigned   TEXT NOT NULL DEFAULT '[]',
                due_date   TEXT,
                note       TEXT,
                starred    INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            retry_safe=True,  # IF NOT EXISTS: replaying it changes nothing
        )
        what = "database and table" if created_database else f"{self._table!r} table"
        return ProvisionResult(
            target_id=self._database_id,
            name=self._database_name,
            detail=f"Created the {what}",
        )

    async def target_columns(self) -> tuple[str, ...]:
        """The table's columns. There is nothing to map, so setup never asks."""
        if not self._database_id:
            return ()
        try:
            return tuple(sorted(await self._table_columns()))
        except D1Error:
            return ()

    def assignee_options(self) -> list[str]:
        """Assignees are free text here, so there is no list to offer."""
        return []

    # ==================== Row <-> Project ====================

    def _to_project(self, row: dict) -> Optional[Project]:
        title = str(row.get("title") or "").strip()
        record_id = str(row.get("id") or "").strip()
        if not record_id or not title:
            return None  # a row we did not write, or an empty one

        due = row.get("due_date")
        fields = ProjectFields(
            title=title,
            status=(str(row["status"]) if row.get("status") else None),
            assigned=_people_in(row.get("assigned")),
            due_date=(to_iso_date(str(due)) or None) if due else None,
            note=(str(row["note"]) if row.get("note") else None),
            starred=bool(row.get("starred")),
        )
        # The stored id *is* the project's id — not a foreign key to map. That is
        # what makes the same project the same project on a second device.
        project = Project(id=record_id, fields=fields)
        project.link_remote(
            NAME, record_id, modified_at=str(row.get("updated_at") or "") or None
        )
        project.set_base(NAME)
        return project

    def _values(self, changes: dict[str, Any]) -> tuple[list[str], list[Any]]:
        """Column names and bound values for the fields actually supplied.

        A field absent from `changes` — or present as None — is left alone, per
        the protocol. An explicitly empty value is the only thing that clears.
        """
        columns: list[str] = []
        values: list[Any] = []
        for name in FIELD_NAMES:
            value = changes.get(name)
            if value is None:
                continue
            if name == "assigned":
                value = _people_out(value)
            elif name == "due_date":
                iso = to_iso_date(value)
                if iso is None:
                    continue  # unparseable: skip rather than store a typo
                value = iso
            elif name == "starred":
                value = 1 if value else 0
            columns.append(name)
            values.append(value)
        return columns, values

    # ==================== CRUD ====================

    async def fetch(self) -> list[Project]:
        """Every project row in the table."""
        result = await self._client.query(
            self._require_database(),
            f"SELECT {_SELECT_COLUMNS} FROM {self._quoted} ORDER BY id LIMIT ?",
            [MAX_ROWS + 1],
            retry_safe=True,
        )
        if len(result.rows) > MAX_ROWS:
            # Returning the first MAX_ROWS would read as "the rest were deleted"
            # and the merge would drop every local copy.
            raise BackendError(
                f"The {self._table!r} table has more than {MAX_ROWS} rows, which "
                "this backend cannot read in one query."
            )
        projects = []
        for row in result.rows:
            project = self._to_project(row)
            if project is not None:
                projects.append(project)
        return projects

    async def create_record(
        self, fields: dict[str, Any], *, project_id: str = ""
    ) -> RecordRef:
        """Insert (or re-insert) a row under the project's own id.

        An upsert rather than an insert, keyed on the id we were handed: that makes
        the write idempotent, so a replay after a lost response cannot duplicate a
        project the way an appended spreadsheet row can.
        """
        record_id = str(project_id or fields.get("id") or "").strip()
        if not record_id:
            raise BackendError("A D1 record needs the project's id")

        columns, values = self._values(fields)
        names = ", ".join(["id", *columns, "updated_at"])
        placeholders = ", ".join(["?"] * (1 + len(columns)) + [_NOW])
        updates = ", ".join(
            [f"{c} = excluded.{c}" for c in columns] + [f"updated_at = {_NOW}"]
        )
        result = await self._client.query(
            self._require_database(),
            f"INSERT INTO {self._quoted} ({names}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates} "
            f"RETURNING id, updated_at",
            [record_id, *values],
            retry_safe=True,
        )
        return RecordRef(id=record_id, modified_at=_stamp_of(result.rows, record_id))

    async def update_record(
        self,
        remote_id: str,
        changes: dict[str, Any],
        *,
        expected_modified_at: Optional[str] = None,
    ) -> None:
        """Write `changes`, refusing if the row moved on since we last saw it."""
        columns, values = self._values(changes)
        if not columns:
            return  # nothing to write

        assignments = ", ".join([f"{c} = ?" for c in columns] + [f"updated_at = {_NOW}"])
        sql = f"UPDATE {self._quoted} SET {assignments} WHERE id = ?"
        params = [*values, remote_id]
        if expected_modified_at:
            sql += " AND updated_at = ?"
            params.append(expected_modified_at)
        sql += " RETURNING id"

        result = await self._client.query(
            self._require_database(),
            sql,
            params,
            # A compare-and-swap must never be replayed: the first attempt may
            # have applied, and the replay would then find the stamp changed and
            # report a conflict that did not happen.
            retry_safe=not expected_modified_at,
        )
        if _applied(result):
            return
        if expected_modified_at:
            raise StaleRecordError(
                "Someone else changed this project since it was last synced — "
                "reloading to merge the two."
            )
        raise BackendError(f"No project with id {remote_id!r} in {self._table!r}")

    async def delete_record(self, remote_id: str) -> None:
        """Delete a row. Deleting an absent row is success, not an error."""
        await self._client.query(
            self._require_database(),
            f"DELETE FROM {self._quoted} WHERE id = ?",
            [remote_id],
            retry_safe=True,
        )

    async def aclose(self) -> None:
        """Close the transport only if this backend created it."""
        if self._owns_client:
            await self._client.aclose()


def _applied(result) -> bool:
    """Whether a write actually matched a row.

    `RETURNING` is the primary signal. `changes` is the fallback for a build that
    does not support it — and a *missing* count is not zero, so an unknown count
    with no returned rows is treated as not applied only when there is nothing
    else to go on.
    """
    if result.rows:
        return True
    changes = result.changes
    return bool(changes)


def _stamp_of(rows: list[dict], record_id: str) -> Optional[str]:
    """The `updated_at` a write reported, if it returned one."""
    for row in rows:
        if str(row.get("id") or "") == record_id and row.get("updated_at"):
            return str(row["updated_at"])
    return None


# A structural check, so a change to the protocol shows up here rather than at
# the first call site.
_: type[Backend] = D1Backend
