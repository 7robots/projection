"""Projection with no backend configured at all.

This is the mode that matters most for publishing: the local store is the source
of record, so someone with no Smartsheet, no D1, and no credentials should get a
fully working project manager.
"""

import asyncio

import pytest

from projection.backends import build_backend
from projection.config import Config, SmartsheetConfig
from projection.local_storage import LocalStorage
from projection.sync import SyncCoordinator


async def _drain():
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
def coord(tmp_path):
    c = SyncCoordinator(on_event=None, poll_interval=0, remote=None, data_dir=tmp_path)
    c._ready = True
    return c


def test_no_backend_is_built_by_default():
    """A fresh config means local-only, not a half-configured integration."""
    assert build_backend(Config()) is None


def test_an_explicit_none_is_honoured():
    assert build_backend(Config(backend="none")) is None


def test_an_unknown_backend_is_a_clear_error():
    from projection.backends import BackendError

    with pytest.raises(BackendError, match="Unknown backend"):
        build_backend(Config(backend="mysql"))


def test_the_coordinator_knows_it_has_no_backend(coord):
    assert coord.has_backend is False
    assert coord.last_sync() is None
    assert coord.assignee_options() == []


# ==================== CRUD works entirely locally ====================


async def test_add_edit_and_delete_all_work(coord):
    project = await coord.add_item("Write the thing", status="In progress")
    assert [p.title for p in coord.load()] == ["Write the thing"]

    assert await coord.update_item(project.id, note="started today")
    assert coord.get_item(project.id).note_text == "started today"

    assert await coord.delete_item(project.id)
    assert coord.load() == []


async def test_nothing_is_ever_marked_dirty(coord):
    """`dirty` means "not yet pushed", and there is nowhere to push to."""
    project = await coord.add_item("Local only")
    assert coord.load()[0].dirty is False

    await coord.update_item(project.id, note="edited")
    assert coord.get_item(project.id).dirty is False


async def test_no_background_writes_are_scheduled(coord):
    project = await coord.add_item("Local only")
    await coord.update_item(project.id, note="edited")
    await coord.delete_item(project.id)
    assert coord._tasks == set()
    assert coord._pending == {}


async def test_a_refresh_is_a_harmless_no_op(coord):
    await coord.add_item("Still here")
    assert [p.title for p in await coord.refresh()] == ["Still here"]
    assert coord.last_error is None


async def test_initial_sync_returns_the_store(coord):
    await coord.add_item("Already here")
    assert [p.title for p in await coord.initial_sync()] == ["Already here"]


async def test_initial_sync_on_an_empty_store_is_empty_not_an_error(coord):
    assert await coord.initial_sync() == []
    assert coord.last_error is None


def test_polling_never_starts(coord):
    coord._poll_interval = 5
    coord.start_polling()
    assert coord._poll_task is None


async def test_records_carry_no_backend_links(coord, tmp_path):
    """Nothing should be written under a backend that doesn't exist."""
    project = await coord.add_item("Local only")
    stored = LocalStorage("projects", storage_dir=tmp_path).get_item(project.id)
    assert stored.remote == {}


async def test_a_delete_still_leaves_a_tombstone(coord):
    """It costs nothing and keeps the store's shape the same in both modes."""
    project = await coord.add_item("Gone")
    await coord.delete_item(project.id)
    assert [s["id"] for s in coord._local.tombstones()] == [project.id]


async def test_conflicts_cannot_arise(coord):
    await coord.add_item("Local only")
    await coord.refresh()
    assert coord.conflicted() == []


# ==================== Config resolution ====================


def test_a_config_with_smartsheet_settings_still_syncs(tmp_path):
    """An install predating the `backend` key must not go quietly local-only."""
    path = tmp_path / "config.toml"
    path.write_text("[smartsheet]\nprojects_sheet_id = 123\n")
    assert Config.load(path).backend == "smartsheet"


def test_a_config_with_neither_is_local_only(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[headless]\nsummary_model = \"haiku\"\n")
    assert Config.load(path).backend == ""


def test_the_written_default_config_is_local_only(tmp_path):
    """A first run must not point at whichever sheet the defaults name."""
    path = tmp_path / "config.toml"
    Config.load(path)  # writes the default file
    assert path.exists()
    # Re-reading what was just written must agree with the first load.
    assert Config.load(path).backend == ""


def test_an_explicit_backend_key_wins_over_inference(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('backend = ""\n[smartsheet]\nprojects_sheet_id = 123\n')
    assert Config.load(path).backend == ""


def test_no_sheet_is_configured_by_default():
    """A built-in sheet id is how a tool ships one team's schema."""
    settings = SmartsheetConfig()
    assert settings.projects_sheet_id == 0
    assert settings.projects_sheet_name == ""
    # And canonical column names, not any particular sheet's vocabulary.
    assert settings.columns["note"] == "Note"


def test_smartsheet_without_a_sheet_is_a_loud_error():
    """Better than falling back to an id and reading a sheet nobody asked for."""
    from projection.backends import BackendError

    with pytest.raises(BackendError, match="no sheet is configured"):
        build_backend(Config(backend="smartsheet"))
