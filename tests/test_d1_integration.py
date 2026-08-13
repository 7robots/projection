"""The D1 backend against a real SQLite database.

The unit tests in `test_d1_backend.py` check the statements the backend *builds*.
These run them, so the SQL itself is exercised: a malformed `ON CONFLICT`, a
column the DDL never created, or a `RETURNING` clause SQLite rejects fails here
instead of in production. D1 is SQLite, which is what makes the substitution fair.

Only the HTTP layer is stubbed — the client's transport is tested separately in
`test_d1_api.py`, and sockets in a unit suite buy flakiness, not coverage.
"""

import sqlite3

import pytest

from projection.backends import StaleRecordError
from projection.backends.d1 import D1Backend
from projection.d1_api import D1Error, QueryResult
from projection.models import Person
from projection.sync import SyncCoordinator

DATABASE = "db-1"


class SqliteClient:
    """A `D1Client` whose queries run against an in-memory SQLite database."""

    def __init__(self, name: str = "projection") -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._name = name
        self.statements: list[str] = []

    async def ensure_ready(self) -> None:
        return None

    async def aclose(self) -> None:
        self._conn.close()

    async def get_database(self, database_id):
        return {"uuid": database_id, "name": self._name}

    async def create_database(self, name):
        self._name = name
        return {"uuid": "new-db", "name": name}

    async def query(self, database_id, sql, params=None, *, retry_safe):
        self.statements.append(" ".join(sql.split()))
        try:
            cursor = self._conn.execute(sql, list(params or []))
            rows = [dict(row) for row in cursor.fetchall()]
            self._conn.commit()
            changes = cursor.rowcount
        except sqlite3.Error as e:
            # Same shape as a Cloudflare-reported SQL error.
            raise D1Error(f"Cloudflare error: {e}")
        return QueryResult(rows=rows, meta={"changes": max(changes, 0)})


@pytest.fixture
async def backend():
    client = SqliteClient()
    made = D1Backend(client, account_id="acct", database_id=DATABASE)
    await made.provision()  # creates the table for real
    yield made
    await client.aclose()


# ==================== The schema the backend writes ====================


async def test_provision_creates_a_table_the_backend_can_use():
    client = SqliteClient()
    made = D1Backend(client, account_id="acct", database_id=DATABASE)

    probe_before = await made.probe()
    assert probe_before.repairable is True

    await made.provision()
    probe_after = await made.probe()
    assert probe_after.ready is True


async def test_every_canonical_field_survives_a_round_trip(backend):
    """The DDL and the read/write paths agree, column by column."""
    await backend.create_record(
        {
            "title": "ZTNA",
            "status": "In progress",
            "assigned": [Person(name="Ada Lovelace", email="al@example.edu")],
            "due_date": "2026-12-30",
            "note": "Cloudflare + Palo Alto PoCs",
            "starred": True,
        },
        project_id="proj-1",
    )

    project = (await backend.fetch())[0]
    assert project.id == "proj-1"
    assert project.title == "ZTNA"
    assert project.status == "In progress"
    assert project.assigned_names == ["Ada Lovelace"]
    assert project.fields.assigned[0].email == "al@example.edu"
    assert project.due_date_iso == "2026-12-30"
    assert project.note_text == "Cloudflare + Palo Alto PoCs"
    assert project.is_starred is True
    assert project.remote[backend.name].modified_at  # stamped server-side


async def test_an_update_leaves_other_columns_alone(backend):
    await backend.create_record(
        {"title": "ZTNA", "note": "first", "starred": True}, project_id="proj-1"
    )
    await backend.update_record("proj-1", {"note": "second"})

    project = (await backend.fetch())[0]
    assert project.note_text == "second"
    assert project.title == "ZTNA"
    assert project.is_starred is True


async def test_the_upsert_is_idempotent(backend):
    """A replay after a lost response must not produce a second project."""
    fields = {"title": "ZTNA", "note": "once"}
    await backend.create_record(fields, project_id="proj-1")
    await backend.create_record(fields, project_id="proj-1")

    rows = await backend.fetch()
    assert len(rows) == 1


# ==================== Compare-and-swap, for real ====================


async def test_a_stale_write_is_refused_and_does_not_apply(backend):
    await backend.create_record({"title": "ZTNA", "note": "theirs"}, project_id="p1")

    with pytest.raises(StaleRecordError):
        await backend.update_record(
            "p1", {"note": "mine"}, expected_modified_at="1999-01-01T00:00:00Z"
        )

    # The refusal is the point: nothing was overwritten.
    assert (await backend.fetch())[0].note_text == "theirs"


async def test_a_current_version_write_applies(backend):
    await backend.create_record({"title": "ZTNA", "note": "first"}, project_id="p1")
    current = (await backend.fetch())[0].remote[backend.name].modified_at

    await backend.update_record(
        "p1", {"note": "second"}, expected_modified_at=current
    )
    assert (await backend.fetch())[0].note_text == "second"


async def test_deleting_an_absent_row_is_not_an_error(backend):
    await backend.delete_record("never-existed")


# ==================== Through the coordinator ====================


@pytest.fixture
async def coord(backend, tmp_path):
    made = SyncCoordinator(
        on_event=None, poll_interval=0, remote=backend, data_dir=tmp_path
    )
    made._ready = True
    yield made


async def _settle():
    import asyncio

    for _ in range(30):
        await asyncio.sleep(0)


async def test_a_project_created_locally_reaches_the_database(coord, backend):
    project = await coord.add_item("ZTNA", status="In progress")
    await _settle()

    stored = coord.get_item(project.id)
    assert stored.remote_id(backend.name) == project.id
    assert stored.dirty is False
    assert [p.title for p in await backend.fetch()] == ["ZTNA"]


async def test_a_delete_removes_the_row_and_leaves_a_tombstone(coord, backend):
    project = await coord.add_item("Gone soon")
    await _settle()
    await coord.delete_item(project.id)
    await _settle()

    assert await backend.fetch() == []
    assert [s["id"] for s in coord._local.tombstones()] == [project.id]


async def test_adoption_pushes_existing_local_work_up(backend, tmp_path):
    """The real flow: run local-only for a while, then connect D1."""
    store = tmp_path / "store"
    local_only = SyncCoordinator(
        on_event=None, poll_interval=0, remote=None, data_dir=store
    )
    local_only._ready = True
    await local_only.add_item("Written before connecting")

    # Setup replaces the coordinator rather than mutating it, so this is exactly
    # what the panel does after the wizard saves.
    connected = SyncCoordinator(
        on_event=None, poll_interval=0, remote=backend, data_dir=store
    )
    report = await connected.adopt()

    assert report.pushed == 1
    assert [p.title for p in await backend.fetch()] == ["Written before connecting"]
    assert connected.load()[0].dirty is False


async def test_two_devices_share_one_project_identity(backend, tmp_path):
    """The row id *is* the project id, so a second store agrees on identity."""
    first = SyncCoordinator(
        on_event=None, poll_interval=0, remote=backend, data_dir=tmp_path / "one"
    )
    first._ready = True
    project = await first.add_item("Shared work")
    await _settle()

    second = SyncCoordinator(
        on_event=None, poll_interval=0, remote=backend, data_dir=tmp_path / "two"
    )
    second._ready = True
    await second.initial_sync()

    assert [p.id for p in second.load()] == [project.id]
