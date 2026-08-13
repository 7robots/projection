"""Tests for the Smartsheet backend."""

import pytest

from projection.backends import FieldMap
from projection.backends.smartsheet import NAME as BACKEND
from projection.backends.smartsheet import SmartsheetBackend
from projection.columns import CANONICAL, SMARTSHEET_LEGACY
from projection.smartsheet_api import SmartsheetError

COL = {
    "Project": 1,
    "Due Date": 2,
    "Assigned To": 3,
    "Status": 4,
    "Sync": 5,
    "Update": 6,
}

SHEET = {
    "columns": [
        {"id": COL["Project"], "title": "Project", "type": "TEXT_NUMBER"},
        {"id": COL["Due Date"], "title": "Due Date", "type": "DATE"},
        {
            "id": COL["Assigned To"],
            "title": "Assigned To",
            "type": "MULTI_CONTACT_LIST",
            "contactOptions": [
                {"name": "Ada Lovelace", "email": "ada@example.com"},
                {"name": "Grace Hopper", "email": "grace@example.com"},
            ],
        },
        {"id": COL["Status"], "title": "Status", "type": "PICKLIST"},
        {"id": COL["Sync"], "title": "Sync", "type": "PICKLIST"},
        {"id": COL["Update"], "title": "Update", "type": "TEXT_NUMBER"},
    ],
    "rows": [
        {
            "id": 4001,
            "cells": [
                {"columnId": COL["Project"], "value": "ZTNA"},
                {"columnId": COL["Due Date"], "value": "2026-12-30"},
                {
                    "columnId": COL["Assigned To"],
                    "value": "Grace Hopper",
                    "objectValue": {
                        "objectType": "MULTI_CONTACT",
                        "values": [
                            {
                                "objectType": "CONTACT",
                                "name": "Grace Hopper",
                                "email": "grace@example.com",
                            }
                        ],
                    },
                },
                {"columnId": COL["Status"], "value": "In progress"},
                {"columnId": COL["Sync"], "value": "Yes"},
                {"columnId": COL["Update"], "value": "Cloudflare + Palo Alto PoCs"},
            ],
        },
        {
            "id": 4002,
            "cells": [
                {"columnId": COL["Project"], "value": "AI Sandbox"},
                {"columnId": COL["Status"], "value": "Done"},
                {"columnId": COL["Sync"], "value": "No"},
            ],
        },
        # A blank spacer row: no project name, must be skipped.
        {"id": 4003, "cells": [{"columnId": COL["Status"], "value": "Not started"}]},
    ],
}


class FakeClient:
    def __init__(self, sheet=None):
        self.sheet = sheet if sheet is not None else SHEET
        self.added = []
        self.updated = []
        self.deleted = []

    async def get_sheet(self, sheet_id, **params):
        return self.sheet

    async def add_rows(self, sheet_id, rows):
        self.added.append(rows)
        # Echo back as Smartsheet does, with an assigned row id.
        return [{"id": 9999, "cells": rows[0]["cells"]}]

    async def update_rows(self, sheet_id, rows):
        self.updated.append(rows)
        return [{"id": rows[0]["id"], "cells": rows[0]["cells"]}]

    async def delete_rows(self, sheet_id, row_ids):
        self.deleted.append(row_ids)

    async def create_sheet(self, name, columns):
        self.created = (name, columns)
        return {"id": 777, "name": name, "columns": columns}


@pytest.fixture
def store():
    return SmartsheetBackend(client=FakeClient(), sheet_id=1)


# ---- reading ------------------------------------------------------------


async def test_load_parses_rows(store):
    projects = await store.fetch()
    assert [p.title for p in projects] == ["ZTNA", "AI Sandbox"]

    ztna = projects[0]
    assert ztna.remote_id(BACKEND) == "4001"
    assert ztna.status == "In progress"
    assert ztna.is_starred
    assert ztna.due_date == "12/30/2026"
    assert ztna.assigned_names == ["Grace Hopper"]
    assert ztna.note_text == "Cloudflare + Palo Alto PoCs"


async def test_load_skips_rows_without_a_title(store):
    assert all(p.remote_id(BACKEND) != "4003" for p in await store.fetch())


async def test_contacts_come_from_column_options(store):
    await store.fetch()
    assert store.assignee_options() == ["Ada Lovelace", "Grace Hopper"]


async def test_load_handles_missing_object_value():
    """A level-1 response has no objectValue; fall back to the joined text."""
    sheet = {
        "columns": SHEET["columns"],
        "rows": [{
            "id": 1,
            "cells": [
                {"columnId": COL["Project"], "value": "X"},
                {"columnId": COL["Assigned To"], "displayValue": "Ada Lovelace, Grace Hopper"},
            ],
        }],
    }
    store = SmartsheetBackend(client=FakeClient(sheet), sheet_id=1)
    projects = await store.fetch()
    assert projects[0].assigned_names == ["Ada Lovelace", "Grace Hopper"]


# ---- writing ------------------------------------------------------------


async def test_add_item_builds_cells_and_returns_the_new_key(store):
    created = await store.create_record({
        "title": "New thing",
        "status": "Not started",
        "assigned": ["Ada Lovelace"],
        "due_date": "1/15/2027",
        "note": "kickoff",
        "starred": True,
    })
    assert created.id == "9999"

    cells = {c["columnId"]: c for c in store._client.added[0][0]["cells"]}
    assert cells[COL["Project"]]["value"] == "New thing"
    assert cells[COL["Status"]]["value"] == "Not started"
    assert cells[COL["Sync"]]["value"] == "Yes"
    assert cells[COL["Due Date"]]["value"] == "2027-01-15"
    assert cells[COL["Assigned To"]]["objectValue"] == {
        "objectType": "MULTI_CONTACT",
        "values": [{
            "objectType": "CONTACT",
            "email": "ada@example.com",
            "name": "Ada Lovelace",
        }],
    }
    assert store._client.added[0][0]["toBottom"] is True


async def test_update_only_writes_named_fields(store):
    await store.fetch()
    await store.update_record("4001", {"status": "Blocked"})
    cells = store._client.updated[0][0]["cells"]
    assert store._client.updated[0][0]["id"] == 4001
    assert [c["columnId"] for c in cells] == [COL["Status"]]


async def test_update_clearing_assignees_sends_empty_value(store):
    await store.fetch()
    await store.update_record("4001", {"assigned": []})
    cells = {c["columnId"]: c for c in store._client.updated[0][0]["cells"]}
    assert cells[COL["Assigned To"]] == {"columnId": COL["Assigned To"], "value": ""}


async def test_update_skips_unparseable_due_date(store):
    """A typo'd date is dropped rather than written as garbage."""
    await store.fetch()
    await store.update_record("4001", {"due_date": "whenever", "status": "Blocked"})
    ids = [c["columnId"] for c in store._client.updated[0][0]["cells"]]
    assert COL["Due Date"] not in ids
    assert COL["Status"] in ids


async def test_update_with_no_fields_is_a_noop(store):
    await store.fetch()
    await store.update_record("4001", {})
    assert store._client.updated == []


async def test_unresolvable_assignee_never_clears_the_cell(store):
    """Failing to resolve a name must not be written as 'unassign everyone'."""
    await store.fetch()
    with pytest.raises(SmartsheetError, match="Not a Smartsheet contact"):
        await store.update_record("4001", {"assigned": ["Nobody At All"]})
    assert store._client.updated == []


async def test_partially_unresolvable_assignees_are_refused(store):
    """Writing only the names that resolved would silently drop the others."""
    await store.fetch()
    with pytest.raises(SmartsheetError, match="Nobody At All"):
        await store.update_record("4001", {"assigned": ["Ada Lovelace", "Nobody At All"]})
    assert store._client.updated == []


async def test_raw_email_assignee_is_accepted(store):
    await store.fetch()
    await store.update_record("4001", {"assigned": ["typed@example.com"]})
    cells = {c["columnId"]: c for c in store._client.updated[0][0]["cells"]}
    assert cells[COL["Assigned To"]]["objectValue"]["values"] == [
        {"objectType": "CONTACT", "email": "typed@example.com"}
    ]


async def test_delete_item(store):
    await store.delete_record("4001")
    assert store._client.deleted == [[4001]]


async def test_write_before_read_loads_columns(store):
    """A write issued before any load still resolves column ids."""
    await store.update_record("4001", {"status": "Blocked"})
    assert store._client.updated[0][0]["cells"][0]["columnId"] == COL["Status"]


async def test_missing_column_is_a_clear_error():
    sheet = {"name": "Team Projects", "columns": [{"id": 1, "title": "Wrong"}], "rows": []}
    store = SmartsheetBackend(client=FakeClient(sheet), sheet_id=1)
    with pytest.raises(SmartsheetError, match="'Project'"):
        await store.update_record("1", {"title": "x"})


def test_backend_name_matches_the_migration_literal():
    """`local_storage` names the same string without importing this module.

    The store must not have to import a backend in order to read a file, so the
    literal is written twice on purpose. This is what keeps the two in step: if
    they diverge, a migrated v2 record's row id becomes unreachable.
    """
    from projection.local_storage import _V2_BACKEND

    assert BACKEND == _V2_BACKEND


# ==================== The field map ====================


async def test_a_mapped_column_vocabulary_is_used_for_reads_and_writes():
    """Adopting a sheet with its own column names needs only a mapping."""
    cols = {"Name": 1, "State": 2, "Owners": 3, "When": 4, "Pinned": 5, "Latest": 6}
    sheet = {
        "name": "Other Sheet",
        "columns": [{"id": i, "title": t} for t, i in cols.items()],
        "rows": [{
            "id": 77,
            "cells": [
                {"columnId": cols["Name"], "value": "Adopted"},
                {"columnId": cols["State"], "value": "Blocked"},
                {"columnId": cols["Latest"], "value": "carried over"},
                {"columnId": cols["Pinned"], "value": "Yes"},
            ],
        }],
    }
    store = SmartsheetBackend(
        client=FakeClient(sheet),
        sheet_id=1,
        field_map=FieldMap({
            "title": "Name",
            "status": "State",
            "assigned": "Owners",
            "due_date": "When",
            "note": "Latest",
            "starred": "Pinned",
        }),
    )

    project = (await store.fetch())[0]
    assert (project.title, project.status) == ("Adopted", "Blocked")
    assert project.note_text == "carried over"
    assert project.is_starred is True

    await store.update_record("77", {"note": "new text"})
    written = store._client.updated[0][0]["cells"]
    assert written == [{"columnId": cols["Latest"], "value": "new text"}]


def test_the_default_map_is_the_legacy_sheet_vocabulary():
    """The flat `[smartsheet]` section only ever described one sheet's columns."""
    store = SmartsheetBackend(client=FakeClient(), sheet_id=1)
    assert store._map.columns == SMARTSHEET_LEGACY


def test_field_map_reports_unmapped_fields():
    partial = FieldMap({"title": "Name"})
    assert "note" in partial.unmapped()
    assert "title" not in partial.unmapped()


def test_field_map_titles_are_in_canonical_order():
    assert FieldMap(dict(CANONICAL)).titles[0] == CANONICAL["title"]


def test_field_map_names_the_missing_table_in_its_error():
    from projection.backends import BackendError

    with pytest.raises(BackendError, match="note"):
        FieldMap({"title": "Name"}).column("note")


# ==================== Probing ====================


async def test_probe_is_ready_for_a_matching_sheet():
    store = SmartsheetBackend(client=FakeClient(), sheet_id=1)
    result = await store.probe()
    assert result.ready is True
    assert result.exists is True


async def test_probe_reports_a_missing_column():
    sheet = {"name": "Team Projects", "columns": [{"id": 1, "title": "Project"}], "rows": []}
    result = await SmartsheetBackend(client=FakeClient(sheet), sheet_id=1).probe()
    assert result.ready is False
    assert result.exists is True
    assert "status" in result.missing_fields
    assert "Status" in result.detail


async def test_probe_refuses_a_sheet_with_the_wrong_name():
    """A plausible-but-wrong sheet id is the failure this catches."""
    store = SmartsheetBackend(
        client=FakeClient({"name": "Someone Else's Sheet", "columns": [], "rows": []}),
        sheet_id=1,
        sheet_name="Team Projects",
    )
    result = await store.probe()
    assert result.ready is False
    assert "Someone Else's Sheet" in result.detail


async def test_probe_reports_an_unreachable_sheet_as_absent():
    class DeadClient(FakeClient):
        async def get_sheet(self, sheet_id, **params):
            raise SmartsheetError("404 not found")

    result = await SmartsheetBackend(client=DeadClient(), sheet_id=1).probe()
    assert result.ready is False
    assert result.exists is False
    assert result.needs_provisioning is True


async def test_probe_reports_no_sheet_as_the_provision_case():
    """`exists=False` is what tells setup to offer creating one."""
    result = await SmartsheetBackend(client=FakeClient(), sheet_id=0).probe()
    assert result.ready is False
    assert result.needs_provisioning is True


# ==================== Provisioning ====================


def _unprovisioned(client, **kwargs):
    return SmartsheetBackend(
        client=client,
        sheet_id=0,
        field_map=FieldMap(dict(CANONICAL)),
        **kwargs,
    )


async def test_provision_creates_a_sheet_with_the_canonical_columns():
    client = FakeClient()
    store = _unprovisioned(client, status_options=("Not started", "Done"))

    result = await store.provision()

    name, columns = client.created
    assert name == "Projection Projects"
    assert [c["title"] for c in columns] == [
        "Title", "Status", "Assigned", "Due Date", "Note", "Starred",
    ]
    # Exactly one primary column, and it is the title: Smartsheet requires one,
    # and a title is the only field every project has.
    assert [c["title"] for c in columns if c.get("primary")] == ["Title"]
    by_title = {c["title"]: c for c in columns}
    assert by_title["Status"]["options"] == ["Not started", "Done"]
    assert by_title["Assigned"]["type"] == "MULTI_CONTACT_LIST"
    assert by_title["Due Date"]["type"] == "DATE"
    # Yes/No text, matching how `starred` is read and written everywhere else.
    assert by_title["Starred"]["options"] == ["Yes", "No"]

    assert result.target_id == "777"
    assert result.name == "Projection Projects"


async def test_provision_uses_the_configured_sheet_name_and_column_titles():
    """A mapping set before provisioning names the columns it creates."""
    client = FakeClient()
    store = SmartsheetBackend(
        client=client,
        sheet_id=0,
        sheet_name="My Work",
        field_map=FieldMap(dict(SMARTSHEET_LEGACY)),
    )
    await store.provision()

    name, columns = client.created
    assert name == "My Work"
    assert [c["title"] for c in columns] == [
        "Project", "Status", "Assigned To", "Due Date", "Update", "Sync",
    ]


async def test_the_backend_is_usable_immediately_after_provisioning():
    """Setup must not have to rebuild it — the id it just got is the target."""
    client = FakeClient({"name": "Projection Projects", "columns": [], "rows": []})
    store = _unprovisioned(client)
    await store.provision()
    assert store.sheet_id == 777
    # Column metadata from the *old* (absent) sheet must not linger.
    assert store._columns == {}


async def test_a_status_column_with_no_options_is_plain_text():
    """A picklist with no options accepts nothing useful."""
    client = FakeClient()
    await _unprovisioned(client, status_options=()).provision()
    status = next(c for c in client.created[1] if c["title"] == "Status")
    assert status["type"] == "TEXT_NUMBER"
    assert "options" not in status


async def test_provision_refuses_when_a_sheet_is_already_configured():
    """Re-running setup must not orphan the rows in the first sheet."""
    from projection.backends import BackendError

    store = SmartsheetBackend(client=FakeClient(), sheet_id=1)
    assert store.capabilities.can_provision is True
    with pytest.raises(BackendError, match="already configured"):
        await store.provision()


async def test_provision_refuses_without_a_full_column_map():
    from projection.backends import BackendError

    store = SmartsheetBackend(
        client=FakeClient(), sheet_id=0, field_map=FieldMap({"title": "Name"})
    )
    with pytest.raises(BackendError, match="status"):
        await store.provision()


async def test_writes_without_a_sheet_say_so_rather_than_404():
    from projection.backends import BackendError

    store = _unprovisioned(FakeClient())
    with pytest.raises(BackendError, match="run setup"):
        await store.fetch()


# ==================== Adopting a sheet's own vocabulary ====================


async def test_target_columns_lists_the_sheets_real_titles():
    """What the mapping dialog offers, independent of the current map."""
    store = SmartsheetBackend(
        client=FakeClient(), sheet_id=1, field_map=FieldMap({"title": "Nope"})
    )
    assert await store.target_columns() == (
        "Project", "Due Date", "Assigned To", "Status", "Sync", "Update",
    )


async def test_target_columns_is_empty_before_a_sheet_exists():
    assert await _unprovisioned(FakeClient()).target_columns() == ()


# ==================== Client ownership ====================


async def test_a_handed_in_client_is_not_closed():
    """Whoever created the client owns it — the panel shares one across sheets."""
    class Tracking(FakeClient):
        closed = False

        async def aclose(self):
            self.closed = True

    client = Tracking()
    await SmartsheetBackend(client=client, sheet_id=1).aclose()
    assert client.closed is False
