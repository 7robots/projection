"""Executive-summary push into the Leadership Roll-up sheet.

Part of the `exec-summary` hook example, not of Projection itself: which sheet
and which row are this team's business. It reuses `projection.smartsheet_api`
for transport and reads its own credentials, because a hook is never handed
Projection's.

Writes the reviewed summary to the IA row's `Update` cell and ticks its
`Status Updated?` checkbox, in one API call. Push-only: nothing is ever read
back from this sheet into Projection's project data.
"""

from typing import Optional

from projection.secrets import SMARTSHEET, Credential
from projection.smartsheet_api import SmartsheetClient, SmartsheetError, columns_by_title

COL_CATEGORY = "Category"
COL_UPDATE = "Update"
COL_STATUS_UPDATED = "Status Updated?"

_REQUIRED_COLUMNS = (COL_CATEGORY, COL_UPDATE, COL_STATUS_UPDATED)


class SummaryStore:
    """Push the executive summary to one row of Leadership Roll-up."""

    def __init__(
        self,
        client: Optional[SmartsheetClient] = None,
        *,
        sheet_id: int,
        row_id: int,
        category: str,
        sheet_name: str,
        token_ref: str = "",
    ) -> None:
        """Every target is required.

        This writes into a shared leadership sheet, so none of the four values
        that identify the destination row may come from a default sitting in
        this module — they are config, and a stale default is how you write
        into someone else's row.
        """
        # Owns the client only when it made one: a caller that shares a client
        # keeps the right to close it.
        self._owns_client = client is None
        # Its own credential reference, from its own config: Projection carries no
        # default one, and a hook is never handed Projection's token.
        self._client = client or SmartsheetClient(
            credential=SMARTSHEET.with_ref(token_ref)
        )
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._category = category
        self._sheet_name = sheet_name

    async def aclose(self) -> None:
        """Close the transport, if this store created it."""
        if self._owns_client:
            await self._client.aclose()

    async def push(self, summary: str) -> int:
        """Write `summary` to the IA row and tick Status Updated?.

        Returns the row id written to.

        This writes into a shared leadership sheet, so the target is confirmed
        three ways before anything is sent — the sheet's name, the row's
        Category, and unambiguous column titles — and the result is read back
        afterwards to confirm both cells actually took.
        """
        sheet = await self._client.get_sheet(self._sheet_id)
        self._verify_sheet(sheet)
        columns = columns_by_title(sheet, _REQUIRED_COLUMNS)

        row_id = self._locate_row(sheet.get("rows", []), columns[COL_CATEGORY])
        await self._client.update_rows(
            self._sheet_id,
            [{
                "id": row_id,
                "cells": [
                    {"columnId": columns[COL_UPDATE], "value": summary},
                    {"columnId": columns[COL_STATUS_UPDATED], "value": True},
                ],
            }],
        )
        await self._verify_write(row_id, columns, summary)
        return row_id

    def _verify_sheet(self, sheet: dict) -> None:
        """Refuse to write unless this really is the expected sheet.

        `_locate_row` guards the row, but a wrong-but-plausible sheet id in
        config.toml would sail past it if that sheet happens to have the same
        columns and an IA row.
        """
        name = (sheet.get("name") or "").strip()
        if name and name.lower() != self._sheet_name.strip().lower():
            raise SmartsheetError(
                f"Refusing to write: sheet {self._sheet_id} is named {name!r}, "
                f"expected {self._sheet_name!r}. Check summary_sheet_id in "
                "config.toml."
            )

    async def _verify_write(
        self, row_id: int, columns: dict[str, int], summary: str
    ) -> None:
        """Read the row back and confirm both cells landed as intended."""
        sheet = await self._client.get_sheet(self._sheet_id)
        row = next((r for r in sheet.get("rows", []) if r.get("id") == row_id), None)
        if row is None:
            raise SmartsheetError(
                f"Wrote the summary but row {row_id} was not found on read-back"
            )

        cells = {c.get("columnId"): c for c in row.get("cells", [])}
        written = cells.get(columns[COL_UPDATE], {}).get("value")
        if (written or "") != summary:
            raise SmartsheetError(
                "The summary cell does not match what was sent — check the IA "
                "row in Smartsheet before relying on it."
            )
        if cells.get(columns[COL_STATUS_UPDATED], {}).get("value") is not True:
            raise SmartsheetError(
                "The summary was written but 'Status Updated?' is not ticked — "
                "tick it manually."
            )

    def _locate_row(self, rows: list[dict], category_column_id: int) -> int:
        """Find the target row, preferring the configured id but verifying it."""

        def category_of(row: dict) -> str:
            for cell in row.get("cells", []):
                if cell.get("columnId") == category_column_id:
                    value = cell.get("displayValue")
                    if value is None:
                        value = cell.get("value")
                    return str(value or "").strip()
            return ""

        wanted = self._category.strip().lower()
        by_id = {row.get("id"): row for row in rows}

        configured = by_id.get(self._row_id)
        if configured is not None:
            found = category_of(configured)
            if found.lower() != wanted:
                raise SmartsheetError(
                    f"Refusing to write: configured row {self._row_id} has "
                    f"Category {found!r}, expected {self._category!r}. Update "
                    "summary_row_id in config.toml."
                )
            return self._row_id

        # The configured row is gone (sheet re-created?) — fall back to the
        # single row carrying the expected Category.
        matches = [r for r in rows if category_of(r).lower() == wanted]
        if len(matches) == 1:
            return matches[0]["id"]
        if not matches:
            raise SmartsheetError(
                f"No row with Category {self._category!r} in Leadership Roll-up"
            )
        raise SmartsheetError(
            f"{len(matches)} rows have Category {self._category!r} — "
            "set summary_row_id in config.toml to disambiguate"
        )
