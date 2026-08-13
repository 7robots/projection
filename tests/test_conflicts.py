"""Tests for detecting, storing, and resolving conflicts."""

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import RadioSet

from projection.local_storage import LocalStorage
from projection.models import Project, ProjectFields, people
from projection.sync import SyncCoordinator
from projection.views import ConflictModal

from test_sync import BACKEND, FakeRemote, _proj, _with_base


async def _drain():
    for _ in range(10):
        await asyncio.sleep(0)


def _coord(tmp_path, remote=None):
    c = SyncCoordinator(
        on_event=None, poll_interval=0, remote=remote or FakeRemote(), data_dir=tmp_path
    )
    c._ready = True
    return c


def _in_conflict(coord, *, mine, theirs, base, field="note"):
    """Set up one project whose `field` both sides changed differently."""
    local = _with_base(
        _proj("ZTNA", remote_id=9, **{field: mine}), **{field: base}
    )
    coord._local.add_item(local)
    coord._remote.projects = [_proj("ZTNA", remote_id=9, **{field: theirs})]
    return local


# ==================== Detection through the coordinator ====================


async def test_a_refresh_records_a_conflict(tmp_path):
    coord = _coord(tmp_path)
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")

    await coord.refresh()

    stored = coord.get_item(local.id)
    assert stored.has_conflicts
    assert stored.conflict_fields() == ["note"]
    assert stored.note_text == "mine"  # the local value stays on display


async def test_a_conflict_survives_a_restart(tmp_path):
    """It waits for the user, not for the process."""
    coord = _coord(tmp_path)
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
    await coord.refresh()

    reopened = LocalStorage("projects", storage_dir=tmp_path)
    assert reopened.get_item(local.id).has_conflicts


async def test_a_conflicted_field_is_not_pushed(tmp_path):
    """The whole point: neither side is overwritten until someone chooses."""
    coord = _coord(tmp_path)
    _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")

    await coord.refresh()
    await _drain()

    assert not [c for c in coord._remote.calls if c[0] == "update"]


async def test_a_conflict_leaves_the_record_not_dirty(tmp_path):
    """Dirty means "push me", and a conflicted field must not be pushed."""
    coord = _coord(tmp_path)
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
    await coord.refresh()
    assert coord.get_item(local.id).dirty is False


async def test_the_conflicted_view_lists_them(tmp_path):
    coord = _coord(tmp_path)
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
    coord._remote.projects.append(_proj("Quiet", remote_id=10))
    await coord.refresh()

    assert [p.id for p in coord.conflicted()] == [local.id]


# ==================== Resolving ====================


async def test_taking_theirs_adopts_their_value_and_pushes_nothing(tmp_path):
    coord = _coord(tmp_path)
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
    await coord.refresh()

    assert await coord.resolve_conflict(local.id, "note", take_theirs=True)
    await _drain()

    stored = coord.get_item(local.id)
    assert stored.note_text == "theirs"
    assert not stored.has_conflicts
    assert not [c for c in coord._remote.calls if c[0] == "update"]


async def test_keeping_mine_pushes_the_local_value(tmp_path):
    coord = _coord(tmp_path)
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
    await coord.refresh()

    assert await coord.resolve_conflict(local.id, "note", take_theirs=False)
    await _drain()

    stored = coord.get_item(local.id)
    assert stored.note_text == "mine"
    assert not stored.has_conflicts
    pushed = [c for c in coord._remote.calls if c[0] == "update"]
    assert pushed and pushed[-1][2] == {"note": "mine"}


async def test_a_resolution_is_not_re_raised_by_the_next_fetch(tmp_path):
    """Either choice moves the base to their value, so it settles for good."""
    for take_theirs in (True, False):
        coord = _coord(tmp_path / str(take_theirs))
        local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
        await coord.refresh()
        await coord.resolve_conflict(local.id, "note", take_theirs=take_theirs)
        await _drain()

        await coord.refresh()
        assert not coord.get_item(local.id).has_conflicts, take_theirs


async def test_resolving_an_unconflicted_field_reports_nothing_to_do(tmp_path):
    coord = _coord(tmp_path)
    project = _proj("ZTNA", remote_id=9)
    coord._local.add_item(project)
    assert await coord.resolve_conflict(project.id, "note", take_theirs=True) is False


async def test_editing_a_conflicted_field_settles_it(tmp_path):
    """Typing a new value is a decision; the conflict must not linger."""
    coord = _coord(tmp_path)
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
    await coord.refresh()

    await coord.update_item(local.id, note="a third thing")
    await _drain()

    stored = coord.get_item(local.id)
    assert stored.note_text == "a third thing"
    assert not stored.has_conflicts
    # And it settles for good, rather than reappearing on the next fetch.
    await coord.refresh()
    assert not coord.get_item(local.id).has_conflicts


def test_resolve_conflict_on_the_model_moves_the_base_either_way():
    for take_theirs in (True, False):
        project = Project(fields=ProjectFields(title="ZTNA", note="mine"))
        project.link_remote(BACKEND, 1)
        project.set_base(BACKEND)
        project.remote[BACKEND].base.note = "agreed"
        project.conflicts["note"] = _conflict()

        assert project.resolve_conflict("note", take_theirs=take_theirs)
        # Their value is acknowledged whichever way it went — that is what stops
        # the same conflict being raised again.
        assert project.base_for(BACKEND).note == "theirs"
        assert not project.has_conflicts


def _conflict():
    from projection.models import FieldConflict

    return FieldConflict(
        backend=BACKEND, mine="mine", theirs="theirs", base="agreed"
    )


def test_keeping_mine_marks_the_record_for_pushing():
    project = Project(fields=ProjectFields(title="ZTNA", note="mine"))
    project.link_remote(BACKEND, 1)
    project.set_base(BACKEND)
    project.conflicts["note"] = _conflict()

    project.resolve_conflict("note", take_theirs=False)
    assert project.dirty is True


# ==================== The chooser ====================


class Host(App):
    def __init__(self, modal):
        super().__init__()
        self._modal = modal
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return iter(())

    async def on_mount(self) -> None:
        self.push_screen(self._modal, lambda r: setattr(self, "result", r))


def _conflicted_project():
    project = Project(
        fields=ProjectFields(title="ZTNA", note="mine", status="On Hold")
    )
    project.link_remote(BACKEND, 1)
    project.set_base(BACKEND)
    from projection.models import FieldConflict

    project.conflicts["note"] = FieldConflict(
        backend=BACKEND, mine="mine", theirs="theirs", base="agreed"
    )
    project.conflicts["status"] = FieldConflict(
        backend=BACKEND, mine="On Hold", theirs="Done", base="In progress"
    )
    return project


async def test_the_chooser_defaults_to_keeping_mine():
    """A dialog that opened by accident must change nothing when applied."""
    modal = ConflictModal(_conflicted_project())
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.action_save()
        await pilot.pause()
    assert app.result == {"note": False, "status": False}


async def test_cancelling_the_chooser_decides_nothing():
    modal = ConflictModal(_conflicted_project())
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.action_cancel()
        await pilot.pause()
    assert app.result is None


async def test_take_all_theirs_selects_every_field():
    modal = ConflictModal(_conflicted_project())
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.action_all_theirs()
        await pilot.pause()
        modal.action_save()
        await pilot.pause()
    assert app.result == {"note": True, "status": True}


async def test_fields_can_be_decided_independently():
    modal = ConflictModal(_conflicted_project())
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#choice-status", RadioSet)._nodes[1].value = True
        await pilot.pause()
        modal.action_save()
        await pilot.pause()
    assert app.result == {"note": False, "status": True}


async def test_the_chooser_shows_both_values_and_the_shared_ancestor():
    modal = ConflictModal(_conflicted_project())
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = " ".join(str(s.render()) for s in modal.query("Static"))
    assert "Mine: mine" in text
    assert "Theirs: theirs" in text
    assert "was: agreed" in text


async def test_the_chooser_starts_focused_on_the_first_field():
    """Matches the edit dialog's contract: the cursor starts in a control."""
    modal = ConflictModal(_conflicted_project())
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == f"choice-{modal.fields[0]}"


def test_conflicted_fields_are_listed_in_canonical_order():
    """Not insertion order — the dialog should read the same way every time."""
    project = _conflicted_project()
    assert project.conflict_fields() == ["status", "note"]


async def test_the_chooser_shows_a_footer_of_its_own_bindings():
    modal = ConflictModal(_conflicted_project())
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert modal.query("Footer")
        # The dialog's own shortcuts must beat the focused RadioSet.
        assert "ctrl+s" in modal.active_bindings
        assert "escape" in modal.active_bindings


# ==================== Timestamps across the migration boundary ====================


def test_the_sync_age_survives_a_naive_stored_stamp():
    """A v2 store's last_sync is naive; `now_stamp()` is aware.

    Subtracting the two raised TypeError inside the panel's repopulate worker,
    which kills the panel rather than mis-rendering one label.
    """
    from projection.widgets import humanize_age, sync_age_seconds

    for stamp in ("2026-08-11T22:39:33.762065", "2026-08-11T22:39:33+00:00"):
        assert sync_age_seconds(stamp) is not None
        assert humanize_age(stamp) != "unknown"


def test_the_sync_age_of_junk_is_unknown_not_a_crash():
    from projection.widgets import humanize_age, sync_age_seconds

    assert sync_age_seconds("whenever") is None
    assert humanize_age("whenever") == "unknown"


async def test_keeping_mine_survives_a_failed_push_without_re_conflicting(tmp_path):
    """The case where moving the base actually matters.

    Resolving "keep mine" moves the base to *their* value: we have seen it. If
    the push then fails and the app restarts, the field must read as a plain
    unsynced local change and be retried — not raise the same conflict again,
    which would ask the user a question they already answered.
    """
    from test_sync import FailingRemote

    coord = _coord(tmp_path, FailingRemote())
    local = _in_conflict(coord, mine="mine", theirs="theirs", base="agreed")
    await coord.refresh()
    await coord.resolve_conflict(local.id, "note", take_theirs=False)
    await _drain()  # the push fails

    stored = coord.get_item(local.id)
    assert stored.dirty is True
    assert stored.base_for(BACKEND).note == "theirs"

    # A fresh coordinator over the same store: no in-memory guards survive.
    restarted = _coord(tmp_path, FakeRemote([_proj("ZTNA", remote_id=9, note="theirs")]))
    assert restarted._pending == {}
    merged = await restarted.refresh()
    after = restarted.get_item(local.id)
    assert not after.has_conflicts, "the answered question was asked again"
    assert after.note_text == "mine"
    assert after.dirty is True  # still queued to push
