"""Tests for the local store of record."""

import json

import pytest

from projection.local_storage import SCHEMA_VERSION, LocalStorage
from projection.models import Person, Project, ProjectFields, people


@pytest.fixture
def store(tmp_path):
    return LocalStorage("projects", storage_dir=tmp_path)


def _proj(title, remote_id=None, backend="smartsheet", **fields):
    project = Project(fields=ProjectFields(title=title, **fields))
    if remote_id is not None:
        project.link_remote(backend, remote_id)
    return project


# ==================== Basic CRUD, keyed by local id ====================


def test_add_and_get_by_id(store):
    p = _proj("ZTNA", remote_id=11, status="In progress")
    store.add_item(p)
    got = store.get_item(p.id)
    assert got is not None
    assert got.status == "In progress"
    assert got.remote_id("smartsheet") == "11"


def test_a_remote_id_is_not_a_key(store):
    """Looking a project up by its backend key must not work — it isn't the id."""
    store.add_item(_proj("ZTNA", remote_id=11))
    assert store.get_item("11") is None


def test_update_by_id(store):
    p = _proj("ZTNA", status="Not started")
    store.add_item(p)
    assert store.update_item(p.id, status="In progress", note="kickoff")
    got = store.get_item(p.id)
    assert got.status == "In progress"
    assert got.note_text == "kickoff"


def test_update_stamps_only_the_fields_written(store):
    p = _proj("ZTNA", status="Not started")
    store.add_item(p)
    store.update_item(p.id, note="kickoff")
    got = store.get_item(p.id)
    assert got.changed_at("note") is not None
    assert got.changed_at("status") is None


def test_update_ignores_fields_left_out(store):
    p = _proj("ZTNA", status="In progress", note="keep me")
    store.add_item(p)
    store.update_item(p.id, title="Renamed")
    got = store.get_item(p.id)
    assert (got.title, got.status, got.note_text) == ("Renamed", "In progress", "keep me")


def test_unstarring_is_not_mistaken_for_absence(store):
    """`starred=False` is a real value; only None means "not supplied"."""
    p = _proj("ZTNA", starred=True)
    store.add_item(p)
    assert store.update_item(p.id, starred=False)
    assert store.get_item(p.id).is_starred is False


def test_update_skips_an_unparseable_date(store):
    p = _proj("ZTNA", due_date="2026-08-20")
    store.add_item(p)
    store.update_item(p.id, due_date="next tuesday")
    assert store.get_item(p.id).due_date_iso == "2026-08-20"


def test_dates_are_normalized_to_iso_on_write(store):
    p = _proj("ZTNA")
    store.add_item(p)
    store.update_item(p.id, due_date="12/30/2026")
    assert store.get_item(p.id).due_date_iso == "2026-12-30"


def test_assignees_are_coerced_on_write(store):
    p = _proj("ZTNA")
    store.add_item(p)
    store.update_item(p.id, assigned=["Al", {"name": "Jeff", "email": "j@x.edu"}])
    got = store.get_item(p.id)
    assert got.assigned_names == ["Al", "Jeff"]
    assert got.fields.assigned[1].email == "j@x.edu"


def test_rename_keeps_identity(store):
    p = _proj("Old Name", remote_id=11, status="In progress")
    store.add_item(p)
    store.update_item(p.id, title="New Name")
    got = store.get_item(p.id)
    assert got.title == "New Name"
    assert got.id == p.id
    assert got.remote_id("smartsheet") == "11"


def test_duplicate_titles_coexist(store):
    a = _proj("Dup", remote_id=1, status="In progress")
    b = _proj("Dup", remote_id=2, status="Done")
    store.add_item(a)
    store.add_item(b)
    assert len(store.load()) == 2
    assert store.get_item(a.id).status == "In progress"
    assert store.get_item(b.id).status == "Done"


def test_a_project_with_no_backend_works(store):
    """Projection has to be useful with no integration at all."""
    p = _proj("Local only", status="Blocked")
    store.add_item(p)
    got = store.get_item(p.id)
    assert got.remote == {}
    assert got.status == "Blocked"
    assert store.has_local_data() is True


def test_load_dedupes_same_id(store):
    p = _proj("A", status="In progress")
    dup = p.model_copy(deep=True)
    dup.fields.title = "B"
    store.save([p, dup])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].title == "A"


# ==================== Backend links ====================


def test_link_remote_keeps_later_edits(store):
    """A backend key arriving late must not discard edits made meanwhile."""
    fresh = _proj("Fresh", status="Not started")
    store.add_item(fresh)
    store.update_item(fresh.id, note="edited mid-flight")

    linked = store.link_remote(fresh.id, "smartsheet", 42)
    assert linked is not None
    assert linked.remote_id("smartsheet") == "42"
    assert linked.note_text == "edited mid-flight"
    # And the id never moved.
    assert store.get_item(fresh.id).remote_id("smartsheet") == "42"


def test_link_remote_missing_returns_none(store):
    assert store.link_remote("nope", "smartsheet", 42) is None


def test_set_clean_clears_dirty_and_snapshots_the_base(store):
    p = _proj("ZTNA", note="synced text")
    p.dirty = True
    store.add_item(p)
    assert store.set_clean(p.id, "smartsheet")
    got = store.get_item(p.id)
    assert got.dirty is False
    assert got.base_for("smartsheet").note == "synced text"


def test_set_clean_snapshots_what_is_stored_now(store):
    """The base is what actually reached the backend, including a late edit."""
    p = _proj("ZTNA", note="first")
    store.add_item(p)
    store.update_item(p.id, note="edited before the write finished")
    store.set_clean(p.id, "smartsheet")
    assert store.get_item(p.id).base_for("smartsheet").note == (
        "edited before the write finished"
    )


# ==================== Tombstones ====================


def test_delete_leaves_a_tombstone(store):
    p = _proj("ZTNA", remote_id=11)
    store.add_item(p)
    assert store.delete_item(p.id)
    assert store.get_item(p.id) is None
    assert not store.delete_item(p.id)

    stones = store.tombstones()
    assert len(stones) == 1
    assert stones[0]["id"] == p.id
    assert stones[0]["remote"]["smartsheet"]["id"] == "11"
    assert stones[0]["synced"] == []


def test_a_tombstone_survives_a_reopen(store, tmp_path):
    """The whole point: a delete that outlives the process."""
    p = _proj("ZTNA", remote_id=11)
    store.add_item(p)
    store.delete_item(p.id)

    reopened = LocalStorage("projects", storage_dir=tmp_path)
    assert [s["id"] for s in reopened.tombstones()] == [p.id]


def test_deleting_a_never_synced_project_records_no_remote_key(store):
    p = _proj("Local only")
    store.add_item(p)
    store.delete_item(p.id)
    assert store.tombstones()[0]["remote"] == {}


def test_mark_tombstone_synced_is_idempotent(store):
    p = _proj("ZTNA", remote_id=11)
    store.add_item(p)
    store.delete_item(p.id)
    assert store.mark_tombstone_synced(p.id, "smartsheet")
    assert store.mark_tombstone_synced(p.id, "smartsheet")
    assert store.tombstones()[0]["synced"] == ["smartsheet"]


def test_saving_projects_preserves_tombstones(store):
    p = _proj("ZTNA", remote_id=11)
    keep = _proj("Keep", remote_id=12)
    store.add_item(p)
    store.add_item(keep)
    store.delete_item(p.id)

    store.save(store.load())  # a refresh rewrites the item list
    assert [s["id"] for s in store.tombstones()] == [p.id]


def test_purge_drops_only_expired_tombstones(store):
    old = _proj("Old", remote_id=1)
    recent = _proj("Recent", remote_id=2)
    store.add_item(old)
    store.add_item(recent)
    store.delete_item(old.id)
    store.delete_item(recent.id)

    # Backdate one of them.
    raw = json.loads(store.path.read_text())
    for stone in raw["tombstones"]:
        if stone["id"] == old.id:
            stone["deleted_at"] = "2020-01-01T00:00:00+00:00"
    store.path.write_text(json.dumps(raw))

    assert store.purge_tombstones() == 1
    assert [s["id"] for s in store.tombstones()] == [recent.id]


def test_purge_with_nothing_expired_does_not_rewrite(store):
    p = _proj("ZTNA", remote_id=11)
    store.add_item(p)
    store.delete_item(p.id)
    before = store.path.read_text()
    assert store.purge_tombstones() == 0
    assert store.path.read_text() == before


# ==================== Sync state ====================


def test_last_sync_is_per_backend(store):
    assert store.get_last_sync() is None
    store.set_last_sync("smartsheet", "2026-08-07T12:00:00+00:00")
    store.set_last_sync("d1", "2026-08-09T12:00:00+00:00")
    assert store.get_last_sync("smartsheet") == "2026-08-07T12:00:00+00:00"
    assert store.get_last_sync("d1") == "2026-08-09T12:00:00+00:00"
    # With no backend named, the most recent — what "synced Xh ago" means.
    assert store.get_last_sync() == "2026-08-09T12:00:00+00:00"


def test_last_sync_of_an_unknown_backend_is_none(store):
    store.set_last_sync("smartsheet")
    assert store.get_last_sync("nowhere") is None


def test_has_local_data_is_false_before_anything_is_stored(store):
    assert store.has_local_data() is False


# ==================== Nothing is ever discarded ====================


def test_a_v2_file_is_migrated_not_dropped(store, tmp_path):
    (tmp_path / "projects.json").write_text(json.dumps({
        "schema": 2,
        "last_sync": "2026-08-11T22:39:33",
        "items": [{
            "row_id": 4001,
            "local_id": "local:abc",
            "dirty": False,
            "title": "ZTNA",
            "properties": {
                "status": "In progress",
                "assigned": ["Grace Hopper"],
                "due_date": "12/30/2026",
                "update": "Cloudflare PoC",
                "sync": "Yes",
            },
        }],
    }))

    projects = store.load()
    assert len(projects) == 1
    p = projects[0]
    # The v2 local_id becomes the permanent id: anything holding it still works.
    assert p.id == "local:abc"
    assert p.remote_id("smartsheet") == "4001"
    assert p.title == "ZTNA"
    assert p.status == "In progress"
    assert p.assigned_names == ["Grace Hopper"]
    assert p.note_text == "Cloudflare PoC"
    assert p.is_starred is True  # sync = "Yes"
    # Display form is unchanged even though storage is now ISO.
    assert p.due_date_iso == "2026-12-30"
    assert p.due_date == "12/30/2026"


def test_migration_is_written_back_once(store, tmp_path):
    (tmp_path / "projects.json").write_text(json.dumps({
        "schema": 2,
        "items": [{"row_id": 1, "local_id": "local:a", "title": "A", "properties": {}}],
    }))
    store.load()

    raw = json.loads((tmp_path / "projects.json").read_text())
    assert raw["schema"] == SCHEMA_VERSION
    assert raw["migrated_from"]["schema"] == 2

    before = (tmp_path / "projects.json").read_text()
    LocalStorage("projects", storage_dir=tmp_path).load()
    assert (tmp_path / "projects.json").read_text() == before


def test_a_clean_v2_record_gets_a_merge_base(store, tmp_path):
    """Its values *are* the synced values, so they are a valid base."""
    (tmp_path / "projects.json").write_text(json.dumps({
        "schema": 2,
        "last_sync": "2026-08-11T22:39:33",
        "items": [{
            "row_id": 1, "local_id": "local:a", "dirty": False,
            "title": "A", "properties": {"update": "synced"},
        }],
    }))
    p = store.load()[0]
    assert p.base_for("smartsheet") is not None
    assert p.base_for("smartsheet").note == "synced"
    assert p.changed_at("title") == "2026-08-11T22:39:33"


def test_a_dirty_v2_record_gets_no_merge_base(store, tmp_path):
    """A base here would assert local == remote and license discarding the edit."""
    (tmp_path / "projects.json").write_text(json.dumps({
        "schema": 2,
        "last_sync": "2026-08-11T22:39:33",
        "last_modified": "2026-08-11T23:00:00",
        "items": [{
            "row_id": 1, "local_id": "local:a", "dirty": True,
            "title": "A", "properties": {"update": "typed but never sent"},
        }],
    }))
    p = store.load()[0]
    assert p.dirty is True
    assert p.base_for("smartsheet") is None
    # And its stamps come from the later edit, not the earlier sync.
    assert p.changed_at("note") == "2026-08-11T23:00:00"


def test_migration_carries_the_last_sync_to_the_backend(store, tmp_path):
    (tmp_path / "projects.json").write_text(json.dumps({
        "schema": 2,
        "last_sync": "2026-08-11T22:39:33",
        "items": [],
    }))
    store.load()
    assert store.get_last_sync("smartsheet") == "2026-08-11T22:39:33"


def test_an_unknown_schema_is_moved_aside_not_overwritten(store, tmp_path):
    """A file from a newer build is someone else's data."""
    original = json.dumps({"schema": 99, "items": [{"whatever": True}]})
    (tmp_path / "projects.json").write_text(original)

    assert store.load() == []
    saved = list(tmp_path.glob("projects.schema-99.*.json"))
    assert len(saved) == 1
    assert saved[0].read_text() == original


def test_the_google_sheets_era_cache_is_moved_aside(store, tmp_path):
    """v1 was title-keyed with no schema key at all."""
    (tmp_path / "projects.json").write_text(
        '{"items": [{"id": "uuid-1", "title": "Old Project"}]}'
    )
    assert store.load() == []
    assert len(list(tmp_path.glob("projects.schema-None.*.json"))) == 1


def test_an_unparseable_file_is_moved_aside(store, tmp_path):
    (tmp_path / "projects.json").write_text("{not json at all")
    assert store.load() == []
    assert len(list(tmp_path.glob("projects.unparseable.*.json"))) == 1


def test_a_record_that_fails_validation_is_carried_through_a_save(store, tmp_path):
    """Skipped for display, but never dropped from the file."""
    good = _proj("Good")
    (tmp_path / "projects.json").write_text(json.dumps({
        "schema": SCHEMA_VERSION,
        "items": [
            good.model_dump(mode="json"),
            {"id": "broken", "fields": {"assigned": "not a list"}},
        ],
    }))

    loaded = store.load()
    assert [p.title for p in loaded] == ["Good"]

    store.save(loaded)
    raw = json.loads((tmp_path / "projects.json").read_text())
    assert raw["unreadable_items"] == [
        {"id": "broken", "fields": {"assigned": "not a list"}}
    ]


def test_migration_keeps_a_copy_of_the_original(store, tmp_path):
    """A migration rewrites the store of record wholesale — keep the original."""
    original = json.dumps({
        "schema": 2,
        "items": [{"row_id": 1, "local_id": "local:a", "title": "A", "properties": {}}],
    })
    (tmp_path / "projects.json").write_text(original)

    store.load()

    kept = list(tmp_path.glob("projects.schema-2.*.json"))
    assert len(kept) == 1
    assert json.loads(kept[0].read_text()) == json.loads(original)
    # And the live file is the migrated one, not the archive.
    assert json.loads((tmp_path / "projects.json").read_text())["schema"] == SCHEMA_VERSION


# ==================== A crafted file cannot choose where it is moved =========


def test_a_traversing_schema_value_is_confined_to_the_data_directory(tmp_path):
    """The rename is done by Projection, so the path is not the file's to pick.

    A stored `"schema": "../../evil"` used to make the quarantine rename land
    outside the data directory.
    """
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    store = LocalStorage("projects", storage_dir=data_dir)
    store.path.write_text(
        json.dumps({"schema": "../../outside/stolen", "items": [{"title": "x"}]})
    )

    assert store.load() == []  # unreadable layout: quarantined, not used

    strays = list(outside.glob("*.json"))
    assert strays == [], f"the store was moved outside the data directory: {strays}"
    # It was moved aside *inside* the sandbox, with the separators neutralised.
    moved = list(data_dir.glob("projects.*.json"))
    assert len(moved) == 1
    # The separators are gone, so the name cannot address anything. (Dots are
    # kept — they are ordinary in a filename and harmless without a separator.)
    assert "/" not in moved[0].name
    assert moved[0].resolve().parent == data_dir.resolve()


def test_an_ordinary_reason_still_names_the_file_readably(tmp_path):
    """The sanitising must not make the archive names useless."""
    store = LocalStorage("projects", storage_dir=tmp_path)
    store.path.write_text(json.dumps({"schema": 99, "items": []}))
    store.load()
    assert list(tmp_path.glob("projects.schema-99.*.json"))


def test_a_long_or_empty_reason_is_still_a_usable_name(tmp_path):
    store = LocalStorage("projects", storage_dir=tmp_path)
    assert store._aside_path("").name.startswith("projects.aside.")
    assert len(store._aside_path("x" * 500).name) < 80
