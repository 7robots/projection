"""Tests for the field-level three-way merge.

The merge is where the data-loss decisions live, so it is tested as a pure
function: no event loop, no store, no backend.
"""

from projection.merge import merge_fields
from projection.models import FIELD_NAMES, ProjectFields, people

BACKEND = "smartsheet"


def _fields(**overrides) -> ProjectFields:
    base = {
        "title": "ZTNA",
        "status": "In progress",
        "assigned": people(["Al"]),
        "due_date": "2026-08-20",
        "note": "the agreed note",
        "starred": False,
    }
    base.update(overrides)
    return ProjectFields(**base)


def _merge(base, mine, theirs, **kw):
    return merge_fields(base=base, mine=mine, theirs=theirs, backend=BACKEND, **kw)


# ==================== Nobody changed anything ====================


def test_untouched_fields_are_left_alone():
    base = _fields()
    out = _merge(base, _fields(), _fields())
    assert out.fields == base
    assert out.conflicts == {}
    assert out.to_push == set()
    assert out.dirty is False


# ==================== One side changed ====================


def test_a_local_only_change_is_kept_and_queued_to_push():
    out = _merge(_fields(), _fields(note="I typed this"), _fields())
    assert out.fields.note == "I typed this"
    assert out.to_push == {"note"}
    assert out.dirty is True
    # The base must NOT move: the change hasn't reached the backend, and moving
    # it would make the field look synced on the next pass.
    assert out.base.note == "the agreed note"


def test_a_remote_only_change_is_adopted():
    out = _merge(_fields(), _fields(), _fields(note="a colleague wrote this"))
    assert out.fields.note == "a colleague wrote this"
    assert out.took_remote == {"note"}
    assert out.to_push == set()
    assert out.dirty is False
    assert out.base.note == "a colleague wrote this"


# ==================== The point of going field-level ====================


def test_different_fields_on_each_side_do_not_conflict():
    """The common case on a shared sheet, and not a conflict at all."""
    out = _merge(
        _fields(),
        _fields(note="my update"),
        _fields(status="Blocked"),
    )
    assert out.fields.note == "my update"
    assert out.fields.status == "Blocked"
    assert out.conflicts == {}
    assert out.to_push == {"note"}
    assert out.took_remote == {"status"}


def test_every_field_can_merge_independently():
    """Whatever the field's type, the same rules apply."""
    out = _merge(
        _fields(),
        _fields(title="mine", starred=True),
        _fields(status="Done", due_date="2026-09-01", assigned=people(["Al", "Jeff"])),
    )
    assert (out.fields.title, out.fields.starred) == ("mine", True)
    assert out.fields.status == "Done"
    assert out.fields.due_date == "2026-09-01"
    assert [p.name for p in out.fields.assigned] == ["Al", "Jeff"]
    assert out.conflicts == {}


# ==================== Both sides changed ====================


def test_the_same_field_changed_differently_is_a_conflict():
    out = _merge(_fields(), _fields(note="mine"), _fields(note="theirs"))

    assert list(out.conflicts) == ["note"]
    conflict = out.conflicts["note"]
    assert (conflict.mine, conflict.theirs) == ("mine", "theirs")
    assert conflict.base == "the agreed note"
    assert conflict.backend == BACKEND


def test_a_conflict_shows_the_local_value_and_pushes_nothing():
    """Nothing is written for a conflicted field until the user chooses."""
    out = _merge(_fields(), _fields(note="mine"), _fields(note="theirs"))
    assert out.fields.note == "mine"
    assert out.to_push == set()
    assert out.dirty is False


def test_a_conflict_leaves_the_base_alone():
    """Otherwise the conflict would vanish on the next fetch, unresolved."""
    out = _merge(_fields(), _fields(note="mine"), _fields(note="theirs"))
    assert out.base.note == "the agreed note"


def test_both_sides_reaching_the_same_value_is_not_a_conflict():
    out = _merge(_fields(), _fields(note="agreed"), _fields(note="agreed"))
    assert out.conflicts == {}
    assert out.fields.note == "agreed"
    assert out.base.note == "agreed"
    assert out.to_push == set()


def test_conflicts_on_several_fields_are_all_reported():
    out = _merge(
        _fields(),
        _fields(note="mine", status="On Hold"),
        _fields(note="theirs", status="Done"),
    )
    assert set(out.conflicts) == {"note", "status"}


def test_a_re_fetch_keeps_when_a_conflict_was_first_noticed():
    """The timestamp is shown to the user; it should not reset every poll."""
    first = _merge(_fields(), _fields(note="mine"), _fields(note="theirs"))
    again = _merge(
        _fields(),
        _fields(note="mine"),
        _fields(note="theirs"),
        existing=first.conflicts,
    )
    assert again.conflicts["note"].detected_at == first.conflicts["note"].detected_at


def test_the_remote_modification_time_is_carried_for_display():
    out = _merge(
        _fields(),
        _fields(note="mine"),
        _fields(note="theirs"),
        theirs_modified_at="2026-08-12T09:00:00Z",
    )
    assert out.conflicts["note"].theirs_modified_at == "2026-08-12T09:00:00Z"


def test_assignee_conflicts_store_names_and_emails_jsonably():
    """A conflict is persisted, so it cannot hold live model objects."""
    out = _merge(
        _fields(),
        _fields(assigned=people([{"name": "Jeff", "email": "j@x.edu"}])),
        _fields(assigned=people(["Sam"])),
    )
    conflict = out.conflicts["assigned"]
    assert conflict.mine == [{"name": "Jeff", "email": "j@x.edu"}]
    assert conflict.theirs == [{"name": "Sam", "email": ""}]


# ==================== No base ====================


def test_without_a_base_the_local_copy_wins_wholesale():
    """Never synced, so nothing can be said about who changed what."""
    out = _merge(None, _fields(note="unsynced"), _fields(note="remote"))
    assert out.fields.note == "unsynced"
    assert out.conflicts == {}
    assert out.dirty is True


def test_without_a_base_nothing_is_reported_as_conflicting():
    """A migrated dirty record would otherwise conflict on every field."""
    out = _merge(None, _fields(), _fields(title="x", status="Done", note="y"))
    assert out.conflicts == {}


def test_without_a_base_only_differing_fields_are_pushed():
    out = _merge(None, _fields(note="unsynced"), _fields())
    assert out.to_push == {"note"}


def test_without_a_base_an_identical_record_needs_no_push():
    out = _merge(None, _fields(), _fields())
    assert out.to_push == set()
    assert out.dirty is False


# ==================== Shape ====================


def test_the_merge_never_mutates_its_inputs():
    base, mine, theirs = _fields(), _fields(note="mine"), _fields(note="theirs")
    snapshots = (base.model_copy(deep=True), mine.model_copy(deep=True),
                 theirs.model_copy(deep=True))
    _merge(base, mine, theirs)
    assert (base, mine, theirs) == snapshots


def test_every_canonical_field_is_considered():
    """A field missing from the loop would silently never merge."""
    for name in FIELD_NAMES:
        changed = _fields()
        setattr(changed, name, _other_value(name))
        out = _merge(_fields(), _fields(), changed)
        assert out.took_remote == {name}, f"{name} was not merged"


def _other_value(name: str):
    return {
        "title": "different",
        "status": "Done",
        "assigned": people(["Someone Else"]),
        "due_date": "2027-01-01",
        "note": "different note",
        "starred": True,
    }[name]
