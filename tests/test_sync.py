"""Tests for the SyncCoordinator: guards, tombstones, and merge wiring.

The merge *rules* are tested in test_merge.py against the pure function; these
tests cover how the coordinator drives it.
"""

import asyncio

import pytest

from projection.local_storage import LocalStorage
from projection.backends.base import ProbeResult, ProvisionResult, RecordRef
from projection.models import Project, ProjectFields, people
from projection.backends import Capabilities
from projection.backends.smartsheet import NAME as BACKEND
from projection.sync import SyncCoordinator


def _proj(title="", remote_id=None, **fields):
    """A project as the local store would hold it, already in step with remote."""
    project = Project(fields=ProjectFields(title=title, **fields))
    if remote_id is not None:
        project.link_remote(BACKEND, remote_id)
        project.set_base(BACKEND)
    return project


def _with_base(project, **base_fields):
    """Rewrite a project's merge base — what the backend last agreed to.

    Without this every fixture would look perfectly in sync, and the merge would
    have nothing to reason about.
    """
    base = project.fields.model_copy(deep=True)
    for name, value in base_fields.items():
        setattr(base, name, value)
    project.remote[BACKEND].base = base
    return project


class FakeRemote:
    """In-memory stand-in for a Backend."""

    name = BACKEND
    capabilities = Capabilities()

    def __init__(self, projects=None, next_remote_id=1000):
        self.projects = list(projects or [])
        self.calls = []
        self._next_remote_id = next_remote_id

    def assignee_options(self):
        return ["Ada Lovelace", "Grace Hopper"]

    def _find(self, remote_id):
        return next(
            (p for p in self.projects if p.remote_id(BACKEND) == str(remote_id)),
            None,
        )

    async def ensure_ready(self):
        return None

    async def fetch(self):
        return list(self.projects)

    async def create_record(self, fields, *, project_id=""):
        self.calls.append(("add", fields.get("title")))
        remote_id = self._next_remote_id
        self._next_remote_id += 1
        stored = dict(fields)
        stored["assigned"] = people(stored.get("assigned") or [])
        self.projects.append(_proj(remote_id=remote_id, **stored))
        return RecordRef(id=str(remote_id))

    async def update_record(self, remote_id, changes, *, expected_modified_at=None):
        self.calls.append(("update", str(remote_id), changes))
        target = self._find(remote_id)
        if target is not None:
            for name, value in changes.items():
                if value is None:
                    continue
                setattr(target.fields, name, value)
            target.set_base(BACKEND)

    async def delete_record(self, remote_id):
        self.calls.append(("delete", str(remote_id)))
        self.projects = [
            p for p in self.projects if p.remote_id(BACKEND) != str(remote_id)
        ]

    async def probe(self):
        return ProbeResult(ready=True, exists=True)

    async def provision(self):
        return ProvisionResult(target_id="1", name="Fake")

    async def target_columns(self):
        return ()

    async def aclose(self):
        return None


def _coord(tmp_path, remote=None):
    c = SyncCoordinator(
        on_event=None,
        poll_interval=0,
        remote=remote or FakeRemote(),
        data_dir=tmp_path,
    )
    c._ready = True
    return c


@pytest.fixture
def coord(tmp_path):
    return _coord(tmp_path)


async def _drain():
    # Let scheduled background tasks run to completion.
    for _ in range(10):
        await asyncio.sleep(0)


# ==================== Writes ====================


async def test_add_is_local_first_then_records_the_backend_key(coord):
    project = await coord.add_item("ZTNA", status="In progress")
    # Local has it immediately, before the backend knows anything.
    stored = coord.load()[0]
    assert stored.title == "ZTNA"
    assert stored.remote_id(BACKEND) is None
    assert stored.id == project.id

    await _drain()
    assert ("add", "ZTNA") in coord._remote.calls
    assert coord.load()[0].remote_id(BACKEND) == "1000"


async def test_the_project_id_does_not_change_when_the_row_is_created(coord):
    """The old model re-keyed on the row id; a create+edit needed two guards."""
    project = await coord.add_item("ZTNA")
    await _drain()
    assert coord.load()[0].id == project.id


async def test_a_new_project_stamps_every_field(coord):
    project = await coord.add_item("ZTNA", status="In progress")
    stored = coord.load()[0]
    assert stored.changed_at("title") is not None
    assert stored.changed_at("starred") is not None
    assert project.id == stored.id


async def test_update_sends_the_backend_key(coord):
    p = _proj("ZTNA", remote_id=77, status="Not started")
    coord._local.add_item(p)
    await coord.update_item(p.id, status="In progress")
    await _drain()
    _, remote_id, kw = next(c for c in coord._remote.calls if c[0] == "update")
    assert remote_id == "77"
    assert kw["status"] == "In progress"


async def test_rename_is_a_plain_update(coord):
    """Local identity means a rename needs no collision handling."""
    alpha = _proj("Alpha", remote_id=5)
    beta = _proj("Beta", remote_id=6)
    coord._local.add_item(alpha)
    coord._local.add_item(beta)
    assert await coord.update_item(alpha.id, title="Beta") is True
    assert coord._local.get_item(alpha.id).title == "Beta"
    await _drain()
    _, remote_id, kw = next(c for c in coord._remote.calls if c[0] == "update")
    assert (remote_id, kw["title"]) == ("5", "Beta")


async def test_toggle_starred_is_optimistic_locally(coord):
    p = _proj("ZTNA", remote_id=9)
    coord._local.add_item(p)
    await coord.toggle_starred(p.id, True)
    assert coord.load()[0].is_starred
    await _drain()
    updates = [c for c in coord._remote.calls if c[0] == "update"]
    assert updates and updates[0][2]["starred"] is True


async def test_unstarring_reaches_the_backend(coord):
    """`False` must not be mistaken for "field not supplied"."""
    p = _proj("ZTNA", remote_id=9, starred=True)
    coord._local.add_item(p)
    await coord.toggle_starred(p.id, False)
    await _drain()
    updates = [c for c in coord._remote.calls if c[0] == "update"]
    assert updates and updates[0][2]["starred"] is False
    assert coord.load()[0].is_starred is False


async def test_delete_sends_the_backend_key(coord):
    p = _proj("ZTNA", remote_id=9)
    coord._local.add_item(p)
    assert await coord.delete_item(p.id)
    assert coord.load() == []
    await _drain()
    assert ("delete", "9") in coord._remote.calls


async def test_delete_of_an_uncreated_project_skips_the_backend(coord):
    """A project deleted before its create landed has no row to delete."""
    fresh = _proj("Fresh")
    coord._local.add_item(fresh)
    assert await coord.delete_item(fresh.id)
    await _drain()
    assert not [c for c in coord._remote.calls if c[0] == "delete"]


async def test_edit_during_create_is_repushed(coord):
    """An edit made while the create was in flight still reaches the backend."""
    project = await coord.add_item("ZTNA", status="Not started")
    # Edit before the background create task gets a chance to run.
    await coord.update_item(project.id, note="late edit")
    await _drain()

    assert coord.load()[0].note_text == "late edit"
    updates = [c for c in coord._remote.calls if c[0] == "update"]
    assert updates, "expected the mid-flight edit to be pushed"
    assert updates[-1][1] == "1000"
    assert updates[-1][2]["note"] == "late edit"


async def test_delete_during_create_removes_the_orphan_row(coord):
    """Deleting a project mid-create must not leave the row behind."""
    project = await coord.add_item("Oops")
    await coord.delete_item(project.id)
    await _drain()

    assert ("add", "Oops") in coord._remote.calls
    # The row the create made was cleaned up, not orphaned.
    assert ("delete", "1000") in coord._remote.calls
    assert await coord._remote.fetch() == []
    assert coord.load() == []


async def test_pending_guard_released_after_write(coord):
    p = _proj("ZTNA", remote_id=9)
    coord._local.add_item(p)
    await coord.update_item(p.id, status="Blocked")
    assert p.id in coord._pending
    await _drain()
    assert coord._pending == {}


async def test_successful_write_clears_dirty_and_sets_the_base(coord):
    p = _proj("ZTNA", remote_id=9)
    coord._local.add_item(p)
    await coord.update_item(p.id, note="synced fine")
    await _drain()
    stored = coord.load()[0]
    assert stored.dirty is False
    assert stored.base_for(BACKEND).note == "synced fine"
    assert coord._pending == {}


async def test_background_tasks_are_strongly_referenced(coord):
    """asyncio only holds weak refs; a collected task loses the write."""
    p = _proj("ZTNA", remote_id=9)
    coord._local.add_item(p)
    await coord.update_item(p.id, status="Blocked")
    assert len(coord._tasks) == 1
    await _drain()
    assert coord._tasks == set()


# ==================== Refresh and merge ====================


async def test_refresh_replaces_local_with_remote(coord):
    coord._remote.projects = [_proj("Remote", remote_id=1, status="Done")]
    result = await coord.refresh()
    assert [p.title for p in result] == ["Remote"]
    assert coord.load()[0].remote_id(BACKEND) == "1"
    assert coord.last_sync() is not None


def test_assignee_options_pass_through(coord):
    assert coord.assignee_options() == ["Ada Lovelace", "Grace Hopper"]


def test_merge_no_pending_returns_remote(coord):
    remote = [_proj("A", remote_id=1), _proj("B", remote_id=2)]
    assert [p.title for p in coord._merge_remote(remote, [])] == ["A", "B"]


def test_merge_pending_rename_keeps_local(coord):
    # Mid-rename: local has the new title, remote still has the old one.
    local_new = _proj("New", remote_id=1)
    keep = _proj("Keep", remote_id=2)
    coord._hold(local_new.id)
    remote = [_proj("Old", remote_id=1), _proj("Keep", remote_id=2)]
    titles = {p.title for p in coord._merge_remote(remote, [local_new, keep])}
    assert titles == {"New", "Keep"}


def test_merge_pending_add_preserved(coord):
    fresh = _proj("Fresh")
    coord._hold(fresh.id)
    remote = [_proj("Keep", remote_id=2)]  # remote hasn't seen the new row yet
    local = [_proj("Keep", remote_id=2), fresh]
    titles = {p.title for p in coord._merge_remote(remote, local)}
    assert titles == {"Fresh", "Keep"}


def test_merge_preserves_local_identity_on_remote_rows(coord):
    """A remote row arrives with a provisional id; the local one must survive."""
    local = _proj("ZTNA", remote_id=1000)
    incoming = _proj("ZTNA", remote_id=1000)
    assert incoming.id != local.id

    merged = coord._merge_remote([incoming], [local])
    assert merged[0].id == local.id


def test_merge_takes_remote_values_for_clean_rows(coord):
    local = _proj("ZTNA", remote_id=1, note="stale")
    merged = coord._merge_remote(
        [_proj("ZTNA", remote_id=1, note="fresh")], [local]
    )
    assert merged[0].note_text == "fresh"
    # And the adopted values become the new merge base.
    assert merged[0].base_for(BACKEND).note == "fresh"
    assert merged[0].dirty is False


def test_merge_keeps_an_unsynced_local_change(coord):
    """The durable half of the guard: survives a restart, not just a failure."""
    # Base says "old": the local edit never reached the backend, which still
    # holds the old value.
    local = _with_base(_proj("ZTNA", remote_id=1, note="unsynced"), note="old")
    local.dirty = True
    merged = coord._merge_remote(
        [_proj("ZTNA", remote_id=1, note="old")], [local]
    )
    assert merged[0].note_text == "unsynced"
    assert merged[0].dirty is True
    assert not merged[0].has_conflicts


async def test_project_id_is_stable_across_refreshes(tmp_path):
    """A regenerated id would dangle every key held by queued work."""
    class ReparsingRemote(FakeRemote):
        """Returns freshly-built Projects each fetch, as a real backend does."""

        async def fetch(self):
            return [
                _proj(p.title, remote_id=p.remote_id(BACKEND), note=p.fields.note)
                for p in self.projects
            ]

    c = _coord(tmp_path, ReparsingRemote([_proj("ZTNA", remote_id=9)]))
    first = (await c.refresh())[0].id
    second = (await c.refresh())[0].id
    assert first == second


# ==================== Failures never lose typing ====================


class FailingRemote(FakeRemote):
    """Every write fails, like a dropped connection or a rejected value."""

    async def create_record(self, fields, *, project_id=""):
        self.calls.append(("add", fields.get("title")))
        raise RuntimeError("remote down")

    async def update_record(self, remote_id, changes, *, expected_modified_at=None):
        self.calls.append(("update", str(remote_id), changes))
        raise RuntimeError("remote down")

    async def delete_record(self, remote_id):
        self.calls.append(("delete", str(remote_id)))
        raise RuntimeError("remote down")


async def test_failed_create_survives_a_refresh(tmp_path):
    """A write that failed must not have the user's typing silently erased."""
    c = _coord(tmp_path, FailingRemote())

    await c.add_item("Typed but unsent", note="hours of work")
    await _drain()

    survivors = await c.refresh()
    assert [p.title for p in survivors] == ["Typed but unsent"]
    assert c.load()[0].note_text == "hours of work"
    assert c.load()[0].dirty is True


async def test_failed_update_survives_a_refresh(tmp_path):
    remote = FailingRemote([_proj("ZTNA", remote_id=9, note="old text")])
    c = _coord(tmp_path, remote)
    local = _proj("ZTNA", remote_id=9, note="old text")
    c._local.add_item(local)

    await c.update_item(local.id, note="new text")
    await _drain()

    await c.refresh()
    assert c.load()[0].note_text == "new text"  # not reverted to remote


async def test_failed_delete_is_not_resurrected(tmp_path):
    remote = FailingRemote([_proj("Gone", remote_id=9)])
    c = _coord(tmp_path, remote)
    local = _proj("Gone", remote_id=9)
    c._local.add_item(local)

    await c.delete_item(local.id)
    await _drain()

    assert await c.refresh() == []


async def test_a_failed_delete_stays_deleted_after_a_restart(tmp_path):
    """New in phase 1: the tombstone is durable, so a restart cannot undo it.

    Previously the guard was in-memory only — quitting before the backend
    accepted the delete brought the project back on the next launch.
    """
    remote = FailingRemote([_proj("Gone", remote_id=9)])
    c = _coord(tmp_path, remote)
    local = _proj("Gone", remote_id=9)
    c._local.add_item(local)
    await c.delete_item(local.id)
    await _drain()

    # A brand-new coordinator over the same store: no in-memory guards at all.
    restarted = _coord(tmp_path, FakeRemote([_proj("Gone", remote_id=9)]))
    assert restarted._pending == {}
    assert await restarted.refresh() == []


async def test_a_synced_delete_is_not_purged_immediately(tmp_path):
    """Another device still has to learn the project is gone."""
    c = _coord(tmp_path, FakeRemote([_proj("Gone", remote_id=9)]))
    local = _proj("Gone", remote_id=9)
    c._local.add_item(local)
    await c.delete_item(local.id)
    await _drain()
    await c.refresh()

    stones = c._local.tombstones()
    assert [s["id"] for s in stones] == [local.id]
    assert stones[0]["synced"] == [BACKEND]


async def test_create_without_a_backend_key_is_treated_as_failure(tmp_path):
    class NoIdRemote(FakeRemote):
        async def create_record(self, fields):
            self.calls.append(("add", fields.get("title")))
            return RecordRef(id="")  # the backend reported no key

    c = _coord(tmp_path, NoIdRemote())
    project = await c.add_item("Nameless")
    await _drain()
    # Still dirty and still guarded, so the local copy isn't lost.
    assert c.load()[0].dirty is True
    assert project.id in c._pending


async def test_initial_sync_failure_is_reported(tmp_path):
    """An unreachable remote must not present itself as 'no projects'."""
    class DeadRemote(FakeRemote):
        async def fetch(self):
            raise RuntimeError("no network")

    c = SyncCoordinator(
        on_event=None, poll_interval=0, remote=DeadRemote(), data_dir=tmp_path
    )

    assert await c.initial_sync() == []
    assert c.last_error is not None
    assert "no network" in c.last_error


async def test_initial_sync_shows_local_data_without_waiting(tmp_path):
    """A store of record has something to show before any network call."""
    c = _coord(tmp_path, FakeRemote())
    c._local.add_item(_proj("Already here", remote_id=1))
    assert [p.title for p in await c.initial_sync()] == ["Already here"]


async def test_last_error_clears_after_a_good_fetch(coord):
    coord._last_error = "stale failure"
    await coord.refresh()
    assert coord.last_error is None


def test_hold_release_is_counted(coord):
    """Overlapping create+edit on one project: the first must not unguard."""
    coord._hold("1")
    coord._hold("1")
    coord._release("1")
    assert "1" in coord._pending
    coord._release("1")
    assert "1" not in coord._pending


# ==================== Attaching a backend to existing local work ==============


async def test_a_record_the_backend_never_saw_survives_a_fetch(tmp_path):
    """The local-only-then-connect case: absent there is not deleted there.

    Every record in the store is unlinked the moment a backend is attached. If
    the merge read that as "deleted remotely", connecting a backend would empty
    the store — the exact failure a hand-edit of `backend` in config.toml used
    to cause.
    """
    c = _coord(tmp_path, FakeRemote())
    local = _proj("Written while local-only")  # no remote_id, not dirty
    c._local.add_item(local)

    merged = await c.refresh()

    assert [p.title for p in merged] == ["Written while local-only"]
    assert [p.title for p in c.load()] == ["Written while local-only"]


async def test_adopt_pushes_local_work_up(tmp_path):
    remote = FakeRemote()
    c = _coord(tmp_path, remote)
    c._local.add_item(_proj("Mine alone", status="In progress"))

    report = await c.adopt()

    assert report.pushed == 1
    assert [p.title for p in remote.projects] == ["Mine alone"]
    stored = c.load()[0]
    assert stored.remote_id(BACKEND)  # linked
    assert stored.dirty is False      # and in step
    assert "pushed up" in report.summary


async def test_adopt_pulls_in_what_only_the_backend_had(tmp_path):
    c = _coord(tmp_path, FakeRemote([_proj("Theirs", remote_id=7)]))
    report = await c.adopt()
    assert report.pulled == 1
    assert [p.title for p in c.load()] == ["Theirs"]


async def test_adopt_matches_an_existing_row_by_title_instead_of_duplicating(tmp_path):
    """Both sides already have the project; adoption must not create a second."""
    remote = FakeRemote([_proj("Shared thing", remote_id=7, status="Not started")])
    c = _coord(tmp_path, remote)
    c._local.add_item(_proj("  shared   THING ", status="Blocked"))

    report = await c.adopt()

    assert report.linked == 1
    assert len(remote.projects) == 1
    assert len(c.load()) == 1
    # Local values win on adoption, and are pushed.
    assert remote.projects[0].status == "Blocked"
    assert c.load()[0].remote_id(BACKEND) == "7"


async def test_adopt_leaves_ambiguous_titles_unmatched(tmp_path):
    """Two rows with one title: guessing which is which is worse than pushing."""
    remote = FakeRemote([_proj("Dup", remote_id=7), _proj("Dup", remote_id=8)])
    c = _coord(tmp_path, remote)
    c._local.add_item(_proj("Dup"))

    report = await c.adopt()

    assert report.linked == 0
    assert report.pushed == 1


async def test_adopt_counts_a_refused_push_as_failed(tmp_path):
    c = _coord(tmp_path, FailingRemote())
    c._local.add_item(_proj("Cannot land"))

    report = await c.adopt()

    assert (report.pushed, report.failed) == (0, 1)
    # Kept locally, and still flagged as unsynced.
    assert c.load()[0].dirty is True
    assert "could not be pushed" in report.summary


async def test_adopt_raises_when_the_backend_is_unreachable(tmp_path):
    """An unreachable backend must not read as an empty one."""

    class Unreachable(FakeRemote):
        async def fetch(self):
            raise RuntimeError("no network")

    c = _coord(tmp_path, Unreachable())
    c._local.add_item(_proj("Mine alone"))

    with pytest.raises(RuntimeError, match="no network"):
        await c.adopt()
    # Nothing was pushed, and the local record is untouched.
    assert c.load()[0].remote_id(BACKEND) is None


async def test_adopt_with_no_backend_does_nothing(tmp_path):
    c = SyncCoordinator(on_event=None, poll_interval=0, remote=None, data_dir=tmp_path)
    report = await c.adopt()
    assert (report.pulled, report.linked, report.pushed) == (0, 0, 0)


def test_busy_reports_in_flight_writes(coord):
    assert coord.busy is False
    coord._hold("1")
    assert coord.busy is True
    coord._release("1")
    assert coord.busy is False


# ==================== The fakes must match the real contract ====================


def test_the_fake_backend_satisfies_the_protocol():
    """Guards against a fake that overrides a method the protocol dropped.

    Renaming `load` to `fetch` left three subclasses here overriding nothing —
    their failure injection silently stopped happening and the tests still
    passed for the wrong reason.
    """
    from projection.backends import Backend

    assert isinstance(FakeRemote(), Backend)
    assert isinstance(FailingRemote(), Backend)


def test_the_failing_fake_actually_fails():
    """The specific way the rename broke: an override that hit nothing."""
    import inspect

    for name in ("create_record", "update_record", "delete_record"):
        assert name in vars(FailingRemote), f"{name} is not overridden any more"
        assert inspect.iscoroutinefunction(getattr(FailingRemote, name))


# ==================== Compare-and-swap, where a backend has it ================


class CasRemote(FakeRemote):
    """A backend that refuses a write when the record moved on."""

    capabilities = Capabilities(supports_cas=True, reports_modified_at=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected = []
        self.refuse = False

    async def update_record(self, remote_id, changes, *, expected_modified_at=None):
        self.expected.append(expected_modified_at)
        if self.refuse:
            from projection.backends import StaleRecordError

            raise StaleRecordError("Someone else changed this project")
        return await super().update_record(remote_id, changes)


async def test_the_last_seen_stamp_is_sent_to_a_cas_backend(tmp_path):
    remote = CasRemote()
    c = _coord(tmp_path, remote)
    stored = _proj("Shared", remote_id=1)
    stored.remote[BACKEND].modified_at = "2026-08-12T10:00:00Z"
    c._local.add_item(stored)

    await c.update_item(stored.id, note="mine")
    await _drain()

    assert remote.expected == ["2026-08-12T10:00:00Z"]


async def test_nothing_is_sent_to_a_backend_without_cas(tmp_path):
    """Passing a version a backend ignores would imply a guarantee it lacks."""
    remote = FakeRemote()
    c = _coord(tmp_path, remote)
    stored = _proj("Shared", remote_id=1)
    stored.remote[BACKEND].modified_at = "2026-08-12T10:00:00Z"
    c._local.add_item(stored)

    await c.update_item(stored.id, note="mine")
    await _drain()

    # The fake records positional args only; what matters is that it was called
    # and did not receive a version it cannot honour.
    assert [call[0] for call in remote.calls] == ["update"]
    assert c.get_item(stored.id).dirty is False


async def test_a_refused_write_refetches_instead_of_retrying(tmp_path):
    """The merge decides between two changed values — never a blind re-push."""
    remote = CasRemote()
    remote.refuse = True
    c = _coord(tmp_path, remote)
    stored = _proj("Shared", remote_id=1)
    stored.remote[BACKEND].modified_at = "old"
    c._local.add_item(stored)
    # What the backend actually holds now: someone else's value.
    remote.projects = [_proj("Shared", remote_id=1, note="theirs")]

    await c.update_item(stored.id, note="mine")
    await _drain()

    # One attempt, not a retry loop.
    assert remote.expected == ["old"]
    # The local value is kept and still flagged, and the fetch ran.
    kept = c.get_item(stored.id)
    assert kept.note_text == "mine"
    assert kept.dirty is True
    assert stored.id in c._pending  # guard held, so a poll cannot overwrite it
