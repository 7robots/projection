"""Tests for the Cloudflare D1 backend.

The fake client is a tiny SQL-shaped stub, not a database: what matters here is
the statements the backend builds and how it reads the answers, particularly the
three places D1 differs from Smartsheet — shared identity, compare-and-swap, and
a structure it can repair itself.
"""

import json

import pytest

from projection.backends import BackendError, StaleRecordError
from projection.backends.d1 import NAME as BACKEND
from projection.backends.d1 import D1Backend
from projection.d1_api import D1Error, QueryResult
from projection.models import Person

ROW = {
    "id": "abc123",
    "title": "ZTNA",
    "status": "In progress",
    "assigned": json.dumps(
        [{"name": "Grace Hopper", "email": "grace@example.com"}]
    ),
    "due_date": "2026-12-30",
    "note": "Cloudflare + Palo Alto PoCs",
    "starred": 1,
    "updated_at": "2026-08-12T10:00:00Z",
}


_UNSET = object()


class FakeClient:
    """Records statements, and answers whatever the test told it to."""

    def __init__(self, rows=None, database=_UNSET, columns=None):
        self._rows = [ROW] if rows is None else rows
        # `database=None` means "not there", which is why the default is a
        # sentinel rather than None.
        self._database = {"name": "projection"} if database is _UNSET else database
        # None means "the table does not exist" — an empty pragma result.
        self._columns = (
            columns
            if columns is not None
            else [
                "id", "title", "status", "assigned", "due_date", "note", "starred",
                "updated_at",
            ]
        )
        self.statements: list[tuple[str, list, bool]] = []
        self.created_databases: list[str] = []
        self.ready = False
        self.applied = True

    async def ensure_ready(self):
        self.ready = True

    async def aclose(self):
        return None

    async def get_database(self, database_id):
        if self._database is None:
            raise D1Error("Cloudflare could not find it (404)")
        return {"uuid": database_id, **self._database}

    async def create_database(self, name):
        self.created_databases.append(name)
        return {"uuid": "new-db", "name": name}

    async def query(self, database_id, sql, params=None, *, retry_safe):
        self.statements.append((" ".join(sql.split()), list(params or []), retry_safe))
        flat = " ".join(sql.split()).lower()
        if "pragma_table_info" in flat:
            return QueryResult([{"name": c} for c in self._columns], {})
        if flat.startswith("select"):
            return QueryResult(list(self._rows), {})
        if flat.startswith("create table"):
            return QueryResult([], {})
        # A write. `applied` says whether it matched a row.
        rows = [{"id": params[-1] if params else "x", "updated_at": "STAMP"}]
        return QueryResult(rows if self.applied else [], {"changes": 1 if self.applied else 0})

    # -- helpers for assertions ------------------------------------------
    @property
    def writes(self):
        return [
            (sql, params, safe)
            for sql, params, safe in self.statements
            if sql.lower().startswith(("insert", "update", "delete"))
        ]


def _backend(client=None, **kwargs):
    kwargs.setdefault("account_id", "acct")
    kwargs.setdefault("database_id", "db-1")
    return D1Backend(client or FakeClient(), **kwargs)


# ==================== Reading ====================


async def test_fetch_parses_a_row():
    projects = await _backend().fetch()
    assert len(projects) == 1
    project = projects[0]
    assert project.title == "ZTNA"
    assert project.status == "In progress"
    assert project.is_starred is True
    assert project.due_date == "12/30/2026"
    assert project.note_text == "Cloudflare + Palo Alto PoCs"
    assert project.assigned_names == ["Grace Hopper"]
    assert project.fields.assigned[0].email == "grace@example.com"


async def test_the_row_id_is_the_projects_own_id():
    """Shared identity: the same project is the same id on every device."""
    project = (await _backend().fetch())[0]
    assert project.id == "abc123"
    assert project.remote_id(BACKEND) == "abc123"
    assert project.remote[BACKEND].modified_at == "2026-08-12T10:00:00Z"
    # What was just read is the merge base for it.
    assert project.base_for(BACKEND) is not None


async def test_a_row_with_no_title_or_no_id_is_skipped():
    client = FakeClient(rows=[{"id": "x", "title": ""}, {"id": "", "title": "orphan"}])
    assert await _backend(client).fetch() == []


async def test_hand_written_assignees_are_read_as_names():
    """A row edited in the dashboard will not contain our JSON."""
    client = FakeClient(rows=[{**ROW, "assigned": "Ada Lovelace, Grace Hopper"}])
    project = (await _backend(client).fetch())[0]
    assert project.assigned_names == ["Ada Lovelace", "Grace Hopper"]


async def test_a_truncated_read_raises_rather_than_looking_like_deletions():
    """`fetch()` returns everything or raises — the merge trusts that."""
    from projection.backends.d1 import MAX_ROWS

    client = FakeClient(rows=[dict(ROW, id=str(n)) for n in range(MAX_ROWS + 1)])
    with pytest.raises(BackendError, match="cannot read in one query"):
        await _backend(client).fetch()


async def test_the_read_is_a_single_safe_select():
    client = FakeClient()
    await _backend(client).fetch()
    sql, params, retry_safe = client.statements[-1]
    assert sql.startswith("SELECT")
    assert retry_safe is True
    assert params == [5001]


# ==================== Writing ====================


async def test_create_is_an_upsert_on_the_projects_own_id():
    """Idempotent, so a replay after a lost response cannot duplicate a project."""
    client = FakeClient()
    ref = await _backend(client).create_record(
        {"title": "New thing", "starred": True}, project_id="proj-1"
    )

    sql, params, retry_safe = client.writes[-1]
    assert sql.startswith("INSERT INTO")
    assert "ON CONFLICT(id) DO UPDATE" in sql
    assert retry_safe is True  # the whole point of the upsert
    assert params[0] == "proj-1"
    assert ref.id == "proj-1"


async def test_create_without_an_id_is_refused():
    with pytest.raises(BackendError, match="needs the project's id"):
        await _backend().create_record({"title": "No id"})


async def test_update_writes_only_the_given_fields():
    client = FakeClient()
    await _backend(client).update_record("abc123", {"note": "moved on"})
    sql, params, _ = client.writes[-1]
    assert "note = ?" in sql
    assert "title" not in sql
    assert params[:2] == ["moved on", "abc123"]


async def test_an_empty_change_set_writes_nothing():
    client = FakeClient()
    await _backend(client).update_record("abc123", {})
    assert client.writes == []


async def test_assignees_round_trip_through_json():
    client = FakeClient()
    await _backend(client).update_record(
        "abc123", {"assigned": [Person(name="Ada Lovelace", email="al@example.edu")]}
    )
    _, params, _ = client.writes[-1]
    assert json.loads(params[0]) == [{"name": "Ada Lovelace", "email": "al@example.edu"}]


async def test_an_unparseable_date_is_skipped_not_stored():
    client = FakeClient()
    await _backend(client).update_record("abc123", {"due_date": "next tuesday"})
    assert client.writes == []


async def test_delete_is_by_id_and_replayable():
    client = FakeClient()
    await _backend(client).delete_record("abc123")
    sql, params, retry_safe = client.writes[-1]
    assert sql.startswith("DELETE FROM")
    assert params == ["abc123"]
    assert retry_safe is True


# ==================== Compare-and-swap ====================


async def test_a_version_check_is_sent_when_one_is_given():
    client = FakeClient()
    await _backend(client).update_record(
        "abc123", {"note": "mine"}, expected_modified_at="2026-08-12T10:00:00Z"
    )
    sql, params, retry_safe = client.writes[-1]
    assert "AND updated_at = ?" in sql
    assert params[-1] == "2026-08-12T10:00:00Z"
    # Never replayed: a replay would find its own write and report a conflict.
    assert retry_safe is False


async def test_a_refused_version_check_raises_stale():
    client = FakeClient()
    client.applied = False
    with pytest.raises(StaleRecordError, match="Someone else changed"):
        await _backend(client).update_record(
            "abc123", {"note": "mine"}, expected_modified_at="old-stamp"
        )


async def test_a_missing_row_without_a_version_check_is_an_ordinary_error():
    """Not a conflict: nothing raced us, the row simply is not there."""
    client = FakeClient()
    client.applied = False
    with pytest.raises(BackendError) as caught:
        await _backend(client).update_record("gone", {"note": "mine"})
    assert not isinstance(caught.value, StaleRecordError)


async def test_the_backend_declares_cas_support():
    """`SyncCoordinator` only sends a version to a backend that says it can use it."""
    assert _backend().capabilities.supports_cas is True


# ==================== Probing and provisioning ====================


async def test_probe_is_ready_for_our_own_table():
    result = await _backend().probe()
    assert (result.ready, result.exists, result.repairable) == (True, True, False)


async def test_no_database_is_the_provision_case():
    result = await _backend(database_id="").probe()
    assert result.needs_provisioning is True


async def test_an_unreachable_database_is_reported_as_absent():
    result = await _backend(FakeClient(database=None)).probe()
    assert (result.ready, result.exists) == (False, False)
    assert "404" in result.detail


async def test_a_database_with_no_table_is_repairable():
    """The one not-ready case this backend can fix without asking anyone."""
    result = await _backend(FakeClient(columns=[])).probe()
    assert (result.ready, result.exists, result.repairable) == (False, True, True)
    assert "no 'projects' table" in result.detail


async def test_someone_elses_table_is_not_repairable():
    """Adding columns to a table we did not create is not setup's call."""
    client = FakeClient(columns=["id", "name", "owner"])
    result = await _backend(client).probe()
    assert (result.ready, result.repairable) == (False, False)
    assert "title" in result.detail


async def test_a_wrong_database_name_is_refused():
    client = FakeClient(database={"name": "someone-elses"})
    result = await _backend(client, database_name="projection").probe()
    assert result.ready is False
    assert "someone-elses" in result.detail


async def test_provision_creates_the_database_and_the_table():
    client = FakeClient()
    backend = _backend(client, database_id="", database_name="")
    result = await backend.provision()

    assert client.created_databases == ["projection"]
    create = next(s for s, _, _ in client.statements if s.startswith("CREATE TABLE"))
    assert 'CREATE TABLE IF NOT EXISTS "projects"' in create
    assert "id TEXT PRIMARY KEY" in create
    assert result.target_id == "new-db"
    assert backend.database_id == "new-db"


async def test_provision_on_an_existing_database_only_creates_the_table():
    """The repair case: the database is there, the table is not."""
    client = FakeClient(columns=[])
    backend = _backend(client, database_id="db-1")
    result = await backend.provision()

    assert client.created_databases == []
    assert any(s.startswith("CREATE TABLE") for s, _, _ in client.statements)
    assert result.target_id == "db-1"
    assert "table" in result.detail


async def test_writes_without_a_database_say_so():
    with pytest.raises(BackendError, match="run setup"):
        await _backend(database_id="").fetch()


async def test_a_dangerous_table_name_is_refused_at_construction():
    """An identifier cannot be bound, so it is validated instead."""
    with pytest.raises(BackendError, match="not a usable table name"):
        _backend(table="projects; DROP TABLE users")


async def test_there_is_nothing_to_map():
    """A D1 table is ours, so setup never shows the column dialog for it."""
    backend = _backend()
    assert await backend.target_columns()  # it can list them
    assert backend.assignee_options() == []


async def test_a_handed_in_client_is_not_closed():
    client = FakeClient()
    closed = []
    client.aclose = lambda: closed.append(True)
    await _backend(client).aclose()
    assert closed == []
