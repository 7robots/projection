"""Tests for the exec-summary push into Leadership Roll-up."""

import pytest

from projection.smartsheet_api import SmartsheetError
from summary_store import SummaryStore

CATEGORY, UPDATE, CHECKBOX = 10, 20, 30

COLUMNS = [
    {"id": CATEGORY, "title": "Category"},
    {"id": UPDATE, "title": "Update"},
    {"id": CHECKBOX, "title": "Status Updated?"},
]


SHEET_NAME = "Leadership Roll-up"


def _row(row_id, category):
    return {"id": row_id, "cells": [{"columnId": CATEGORY, "value": category}]}


def _sheet(rows=None, columns=None, name=SHEET_NAME):
    return {
        "name": name,
        "columns": columns if columns is not None else COLUMNS,
        "rows": rows if rows is not None else [
            _row(100, "Collaboration"), _row(200, "IA"), _row(300, "Network")
        ],
    }


class FakeClient:
    """Applies writes to its in-memory sheet, so read-back verification works."""

    def __init__(self, sheet=None):
        self.sheet = sheet if sheet is not None else _sheet()
        self.updated = []

    async def get_sheet(self, sheet_id, **params):
        return self.sheet

    async def update_rows(self, sheet_id, rows):
        self.updated.append(rows)
        for written in rows:
            row = next(
                (r for r in self.sheet["rows"] if r["id"] == written["id"]), None
            )
            if row is None:
                continue
            cells = {c["columnId"]: c for c in row["cells"]}
            for cell in written["cells"]:
                cells[cell["columnId"]] = dict(cell)
            row["cells"] = list(cells.values())
        return rows


class LosingClient(FakeClient):
    """Accepts the write but doesn't apply it (silent Smartsheet no-op)."""

    async def update_rows(self, sheet_id, rows):
        self.updated.append(rows)
        return rows


@pytest.fixture
def client():
    return FakeClient()


def _store(client, row_id=200):
    return SummaryStore(
        client=client,
        sheet_id=1,
        row_id=row_id,
        category="IA",
        sheet_name=SHEET_NAME,
    )


async def test_push_writes_update_and_ticks_checkbox(client):
    row_id = await _store(client).push("Infrastructure and Security:\n\nZTNA: ...")
    assert row_id == 200

    written = client.updated[0][0]
    assert written["id"] == 200
    cells = {c["columnId"]: c["value"] for c in written["cells"]}
    assert cells[UPDATE] == "Infrastructure and Security:\n\nZTNA: ..."
    assert cells[CHECKBOX] is True


async def test_push_writes_verbatim(client):
    text = "  leading space kept\n\n* bullets * and : colons"
    await _store(client).push(text)
    cells = {c["columnId"]: c["value"] for c in client.updated[0][0]["cells"]}
    assert cells[UPDATE] == text


async def test_refuses_when_configured_row_is_not_ia(client):
    """A row id pointing at another service area must never be written."""
    with pytest.raises(SmartsheetError, match="expected 'IA'"):
        await _store(client, row_id=300).push("summary")
    assert client.updated == []


async def test_falls_back_to_category_when_row_id_is_gone(client):
    """If the sheet was re-created, find the IA row by its Category."""
    row_id = await _store(client, row_id=999999).push("summary")
    assert row_id == 200
    assert client.updated[0][0]["id"] == 200


async def test_errors_when_no_ia_row_exists():
    client = FakeClient(_sheet(rows=[_row(100, "Network")]))
    with pytest.raises(SmartsheetError, match="No row with Category 'IA'"):
        await _store(client, row_id=999999).push("summary")
    assert client.updated == []


async def test_errors_when_ia_row_is_ambiguous():
    client = FakeClient(_sheet(rows=[_row(100, "IA"), _row(200, "IA")]))
    with pytest.raises(SmartsheetError, match="2 rows have Category"):
        await _store(client, row_id=999999).push("summary")
    assert client.updated == []


async def test_missing_column_is_a_clear_error():
    client = FakeClient(_sheet(columns=[{"id": 1, "title": "Category"}], rows=[]))
    with pytest.raises(SmartsheetError, match="'Update'"):
        await _store(client).push("summary")
    assert client.updated == []


async def test_refuses_a_sheet_with_the_wrong_name():
    """A mistyped sheet id can otherwise pass every row-level check."""
    client = FakeClient(_sheet(name="Some Other Team's Sheet"))
    with pytest.raises(SmartsheetError, match="expected 'Leadership Roll-up'"):
        await _store(client).push("summary")
    assert client.updated == []


async def test_refuses_duplicate_update_columns():
    """Two columns titled 'Update' would silently resolve to one of them."""
    columns = COLUMNS + [{"id": 99, "title": "Update"}]
    client = FakeClient(_sheet(columns=columns))
    with pytest.raises(SmartsheetError, match="more than one column titled"):
        await _store(client).push("summary")
    assert client.updated == []


async def test_detects_a_write_that_did_not_take():
    """The push is not live-tested, so a silent no-op must not report success."""
    client = LosingClient()
    with pytest.raises(SmartsheetError, match="does not match what was sent"):
        await _store(client).push("summary")


async def test_detects_an_unticked_checkbox():
    class NoCheckboxClient(FakeClient):
        async def update_rows(self, sheet_id, rows):
            trimmed = [
                {
                    "id": r["id"],
                    "cells": [c for c in r["cells"] if c["columnId"] != CHECKBOX],
                }
                for r in rows
            ]
            return await super().update_rows(sheet_id, trimmed)

    client = NoCheckboxClient()
    with pytest.raises(SmartsheetError, match="not ticked"):
        await _store(client).push("summary")
