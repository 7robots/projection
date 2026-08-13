"""The Smartsheet backend.

Maps sheet rows onto canonical `Project` fields and back, through a `FieldMap` so
the sheet's own column vocabulary stays in config rather than in code. The row id
is *not* a project's identity — `Project.id` is — and is recorded in
`remote["smartsheet"].id`.

Columns Projection has no mapping for are never read or written, so an adopted
sheet keeps whatever else it holds — formulas, hidden columns, a team's own
priority field — untouched.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from ..columns import SMARTSHEET_LEGACY as LEGACY_COLUMNS
from ..models import (
    FIELD_NAMES,
    Person,
    Project,
    ProjectFields,
    people,
    to_iso_date,
)
from ..smartsheet_api import SmartsheetClient, SmartsheetError, columns_by_title
from .base import (
    Backend,
    BackendError,
    Capabilities,
    FieldMap,
    ProbeResult,
    ProvisionResult,
    RecordRef,
)

NAME = "smartsheet"

# Smartsheet stores the starred flag as picklist text, not a checkbox.
_STARRED_YES = "Yes"
_STARRED_NO = "No"

# What a sheet Projection creates for itself is called, when nothing else is
# given. Generic on purpose: no team's vocabulary ships in the package.
DEFAULT_SHEET_NAME = "Projection Projects"


def _iso_from_cell(raw: object) -> Optional[str]:
    """A date cell's value as a canonical ISO date.

    Smartsheet returns either "2026-08-20" or a full timestamp
    ("2026-08-06T18:04:40Z") depending on the column, so the date part is taken
    before parsing.
    """
    if raw is None or raw == "":
        return None
    return to_iso_date(str(raw)[:10]) or None


def _contacts_from_cell(cell: dict) -> list[Person]:
    """Extract assignees from a contact cell, however it came back."""
    obj = cell.get("objectValue")
    if isinstance(obj, dict):
        values = (
            obj.get("values") or []
            if obj.get("objectType") == "MULTI_CONTACT"
            else [obj]
        )
        contacts: list[Person] = []
        for entry in values:
            if not isinstance(entry, dict):
                continue
            email = entry.get("email") or ""
            name = entry.get("name") or email
            if name or email:
                contacts.append(Person(name=name, email=email))
        return contacts

    # No objectValue (e.g. a level-1 response): fall back to the joined text.
    text = cell.get("displayValue") or cell.get("value") or ""
    return [Person(name=part.strip()) for part in str(text).split(",") if part.strip()]


class SmartsheetBackend:
    """Read and write projects in a Smartsheet."""

    name = NAME
    capabilities = Capabilities(
        row_addressable=True,
        # Smartsheet has no If-Match; two writers can still lose each other's
        # cell values, which is exactly why the merge keeps a base.
        supports_cas=False,
        can_provision=True,
        typed_contacts=True,
        reports_modified_at=True,
    )

    def __init__(
        self,
        client: Optional[SmartsheetClient] = None,
        *,
        sheet_id: int,
        sheet_name: str = "",
        field_map: Optional[FieldMap] = None,
        status_options: Sequence[str] = (),
    ) -> None:
        """The sheet id is required — except when provisioning, which assigns it.

        Zero means "no sheet yet": every read and write path fails loudly on it,
        so the only useful thing to do with such a backend is `provision()`.
        `status_options` is the picklist a provisioned Status column gets; it is
        the configured list, so a sheet Projection creates accepts exactly the
        statuses the UI offers.
        """
        self._owns_client = client is None
        self._client = client or SmartsheetClient()
        self._sheet_id = sheet_id
        self._sheet_name = sheet_name
        self._map = field_map or FieldMap(dict(LEGACY_COLUMNS))
        self._status_options = tuple(status_options)
        self._columns: dict[str, int] = {}
        # Display name -> email, for writing the multi-contact column.
        self._contacts: dict[str, str] = {}

    @property
    def sheet_id(self) -> int:
        """Which sheet this backend is pointed at (0 before provisioning)."""
        return self._sheet_id

    @property
    def sheet_name(self) -> str:
        """The sheet's name as last seen, or as configured."""
        return self._sheet_name

    # ==================== Column / contact metadata ====================

    def _require_sheet(self) -> int:
        """The sheet id, or a clear error rather than a 404 from the API."""
        if not self._sheet_id:
            raise BackendError(
                "No Smartsheet sheet is configured — run setup to create or "
                "connect one."
            )
        return self._sheet_id

    def _column_id(self, field_name: str) -> int:
        title = self._map.column(field_name)
        try:
            return self._columns[title]
        except KeyError:
            raise SmartsheetError(
                f"Sheet {self._sheet_id} has no column named {title!r} (mapped "
                f"from {field_name!r}) — has the sheet layout changed?"
            )

    def _absorb_columns(self, sheet: dict) -> None:
        # Fails loudly on a missing or duplicated title rather than silently
        # resolving a write to the wrong column.
        self._columns = columns_by_title(sheet, self._map.titles)
        assigned_title = self._map.columns.get("assigned")
        for col in sheet.get("columns", []):
            if col.get("title") != assigned_title:
                continue
            for option in col.get("contactOptions") or []:
                name, email = option.get("name"), option.get("email")
                if name and email:
                    self._contacts[name] = email

    def assignee_options(self) -> list[str]:
        """Assignable contact names, for the edit modal's picker."""
        return sorted(self._contacts)

    def _resolve_contact(self, person: Person) -> Optional[dict]:
        """Turn an assignee into the contact object Smartsheet expects.

        The person's own email wins. Carrying it on the record is what makes an
        assignee survive a round trip even when the sheet's `contactOptions` no
        longer lists them — keeping names only made an unrecognised name fail the
        whole write.
        """
        name = (person.name or "").strip()
        email = (person.email or "").strip()
        if not name and not email:
            return None
        if email:
            return {"objectType": "CONTACT", "email": email, "name": name or email}
        known = self._contacts.get(name)
        if known:
            return {"objectType": "CONTACT", "email": known, "name": name}
        if "@" in name:  # user typed an address directly
            return {"objectType": "CONTACT", "email": name}
        return None

    # ==================== Probing ====================

    async def ensure_ready(self) -> None:
        """Load the Smartsheet token and open the session."""
        await self._client.ensure_ready()

    async def probe(self) -> ProbeResult:
        """Check the sheet exists and carries every mapped column."""
        if not self._sheet_id:
            # `exists=False` is what tells setup this is the provision case
            # rather than a broken configuration.
            return ProbeResult(
                ready=False,
                exists=False,
                detail="No sheet chosen yet — Projection can create one.",
            )
        unmapped = self._map.unmapped()
        if unmapped:
            return ProbeResult(
                ready=False,
                exists=True,
                missing_fields=unmapped,
                detail=(
                    "No column mapped for: " + ", ".join(unmapped) +
                    ". Add them under [backends.smartsheet.columns]."
                ),
            )
        try:
            sheet = await self._client.get_sheet(self._sheet_id)
        except SmartsheetError as e:
            return ProbeResult(ready=False, exists=False, detail=str(e))

        found = {c.get("title") for c in sheet.get("columns", [])}
        missing = tuple(
            name for name in FIELD_NAMES if self._map.column(name) not in found
        )
        name = (sheet.get("name") or "").strip()
        if self._sheet_name and name and name.lower() != self._sheet_name.lower():
            return ProbeResult(
                ready=False,
                exists=True,
                detail=(
                    f"Sheet {self._sheet_id} is named {name!r}, expected "
                    f"{self._sheet_name!r} — check the configured sheet id."
                ),
            )
        if missing:
            return ProbeResult(
                ready=False,
                exists=True,
                missing_fields=missing,
                detail=(
                    "The sheet has no column for: "
                    + ", ".join(f"{f} ({self._map.column(f)})" for f in missing)
                ),
            )
        return ProbeResult(ready=True, exists=True, detail=f"{name or 'sheet'} ready")

    async def provision(self) -> ProvisionResult:
        """Create a sheet with the columns Projection needs.

        Column titles come from the same `FieldMap` the read and write paths
        use, so a sheet Projection creates and a sheet Projection adopts agree by
        construction — there is no second list of names to drift.

        Refuses when a sheet is already configured. Creating a second sheet
        because setup was re-run would leave the first one's rows behind with
        nothing pointing at them.
        """
        if self._sheet_id:
            raise BackendError(
                f"Sheet {self._sheet_id} is already configured. Clear `sheet_id` "
                "first if you really want a new sheet."
            )
        unmapped = self._map.unmapped()
        if unmapped:
            raise BackendError(
                "Cannot create a sheet without a column name for: "
                + ", ".join(unmapped)
            )

        name = self._sheet_name or DEFAULT_SHEET_NAME
        created = await self._client.create_sheet(name, self._provision_columns())
        self._sheet_id = int(created["id"])
        self._sheet_name = str(created.get("name") or name)
        # The new sheet's column ids are not the old ones; drop the metadata so
        # the first write reads it back rather than writing to stale ids.
        self._columns = {}
        return ProvisionResult(
            target_id=str(self._sheet_id),
            name=self._sheet_name,
            detail=f"Created {self._sheet_name!r} (sheet {self._sheet_id})",
        )

    def _provision_columns(self) -> list[dict]:
        """Smartsheet column definitions for a new sheet, in canonical order."""
        column = self._map.column
        status: dict[str, Any] = {"title": column("status")}
        if self._status_options:
            status |= {"type": "PICKLIST", "options": list(self._status_options)}
        else:
            # A picklist with no options accepts nothing useful; plain text at
            # least round-trips whatever statuses turn up.
            status["type"] = "TEXT_NUMBER"
        return [
            # Smartsheet requires exactly one primary column, and the title is
            # the only field every project always has.
            {"title": column("title"), "type": "TEXT_NUMBER", "primary": True},
            status,
            {"title": column("assigned"), "type": "MULTI_CONTACT_LIST"},
            {"title": column("due_date"), "type": "DATE"},
            {"title": column("note"), "type": "TEXT_NUMBER"},
            # Text, matching how the flag is read and written everywhere else.
            {
                "title": column("starred"),
                "type": "PICKLIST",
                "options": [_STARRED_YES, _STARRED_NO],
            },
        ]

    async def target_columns(self) -> tuple[str, ...]:
        """Every column title the sheet has, in sheet order.

        Independent of the field map on purpose: this is what the mapping dialog
        offers *because* the map is wrong, so a missing mapping must not stop it
        from answering.
        """
        if not self._sheet_id:
            return ()
        # One row is enough — the columns come back regardless, and a wide sheet
        # with thousands of rows is a slow way to ask for six titles.
        sheet = await self._client.get_sheet(self._sheet_id, pageSize=1)
        return tuple(
            str(col["title"])
            for col in sheet.get("columns", [])
            if col.get("title")
        )

    # ==================== Row <-> Project ====================

    def _parse_row(self, row: dict) -> Optional[Project]:
        by_id = {c.get("columnId"): c for c in row.get("cells", [])}

        def text(field_name: str) -> str:
            cell = by_id.get(self._columns.get(self._map.columns.get(field_name, "")))
            if not cell:
                return ""
            value = cell.get("displayValue")
            if value is None:
                value = cell.get("value")
            return "" if value is None else str(value)

        title = text("title").strip()
        if not title:
            return None  # blank spacer row

        assigned_cell = (
            by_id.get(self._columns.get(self._map.columns.get("assigned", ""))) or {}
        )
        contacts = _contacts_from_cell(assigned_cell)
        for contact in contacts:  # learn contacts that aren't column options
            if contact.name and contact.email:
                self._contacts.setdefault(contact.name, contact.email)

        raw_due = (
            by_id.get(self._columns.get(self._map.columns.get("due_date", "")), {})
            .get("value")
        )
        fields = ProjectFields(
            title=title,
            status=text("status") or None,
            assigned=[c for c in contacts if c.name],
            due_date=_iso_from_cell(raw_due),
            note=text("note") or None,
            starred=text("starred").strip().lower() == _STARRED_YES.lower(),
        )

        project = Project(fields=fields)
        project.link_remote(NAME, row.get("id"), modified_at=row.get("modifiedAt"))
        # What was just read *is* the merge base for it. The local id here is
        # provisional — the caller swaps in the id of whichever local record
        # already maps to this row.
        project.set_base(NAME)
        return project

    def _cells(self, changes: dict[str, Any]) -> list[dict]:
        """Build the cell list for a write from canonical field changes.

        A field absent from `changes` is left alone. `None` means the same thing:
        callers pass a full keyword set with unset fields as None.
        """
        cells: list[dict] = []

        for name in ("title", "status", "note"):
            value = changes.get(name)
            if value is not None:
                cells.append({"columnId": self._column_id(name), "value": value})

        starred = changes.get("starred")
        if starred is not None:
            cells.append({
                "columnId": self._column_id("starred"),
                "value": _STARRED_YES if starred else _STARRED_NO,
            })

        due_date = changes.get("due_date")
        if due_date is not None:
            iso = to_iso_date(due_date)
            if iso is not None:  # an unparseable date is skipped, not written
                cells.append({"columnId": self._column_id("due_date"), "value": iso})

        assigned = changes.get("assigned")
        if assigned is not None:
            column_id = self._column_id("assigned")
            if not assigned:
                # An explicitly empty list is the only thing that means
                # "unassign". A failure to resolve names never clears the cell.
                cells.append({"columnId": column_id, "value": ""})
            else:
                resolved, unresolved = [], []
                for person in people(assigned):
                    contact = self._resolve_contact(person)
                    if contact:
                        resolved.append(contact)
                    else:
                        unresolved.append(person.name or person.email)
                if unresolved:
                    # Writing only the names that resolved would silently drop
                    # the rest, so refuse the whole cell and name the offenders.
                    raise SmartsheetError(
                        "Not a Smartsheet contact on this sheet: "
                        f"{', '.join(unresolved)}. Pick from the assignee list "
                        "or use a full email address."
                    )
                cells.append({
                    "columnId": column_id,
                    "objectValue": {
                        "objectType": "MULTI_CONTACT",
                        "values": resolved,
                    },
                })
        return cells

    # ==================== CRUD ====================

    async def fetch(self) -> list[Project]:
        """Every project row in the sheet."""
        sheet = await self._client.get_sheet(self._require_sheet())
        self._absorb_columns(sheet)
        projects = []
        for row in sheet.get("rows", []):
            project = self._parse_row(row)
            if project is not None:
                projects.append(project)
        return projects

    async def _ensure_columns(self) -> None:
        """Load column metadata if a write happens before any read."""
        if not self._columns:
            await self.fetch()

    async def create_record(
        self, fields: dict[str, Any], *, project_id: str = ""
    ) -> RecordRef:
        """Append a row and return the key Smartsheet assigned it.

        `project_id` is ignored: a row id is Smartsheet's to assign, and there is
        nowhere on the row to put a foreign one that the rest of the sheet would
        not treat as a column of data.
        """
        await self._ensure_columns()
        rows = await self._client.add_rows(
            self._require_sheet(), [{"toBottom": True, "cells": self._cells(fields)}]
        )
        if not rows:
            raise SmartsheetError("Smartsheet did not return the created row")
        created = rows[0]
        remote_id = created.get("id")
        if remote_id is None:
            raise SmartsheetError("Smartsheet returned a created row with no id")
        return RecordRef(id=str(remote_id), modified_at=created.get("modifiedAt"))

    async def update_record(
        self,
        remote_id: str,
        changes: dict[str, Any],
        *,
        expected_modified_at: Optional[str] = None,
    ) -> None:
        """Write just `changes` onto an existing row.

        `expected_modified_at` is ignored, and `supports_cas` says so: Smartsheet
        offers no If-Match, so a concurrent write cannot be *refused* here — it is
        caught after the fact by the merge, which is why the base matters so much
        for this backend.
        """
        await self._ensure_columns()
        cells = self._cells(changes)
        if not cells:
            return  # nothing to write
        await self._client.update_rows(
            self._require_sheet(), [{"id": int(remote_id), "cells": cells}]
        )

    async def delete_record(self, remote_id: str) -> None:
        """Delete a row."""
        await self._client.delete_rows(self._require_sheet(), [int(remote_id)])

    async def aclose(self) -> None:
        """Close the transport only if this backend created it."""
        if self._owns_client:
            await self._client.aclose()


# A structural check, so a change to the protocol shows up here rather than at
# the first call site.
_: type[Backend] = SmartsheetBackend
