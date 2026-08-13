"""Tests for the canonical Project model."""

from projection.models import (
    FIELD_NAMES,
    Person,
    Project,
    ProjectFields,
    people,
    sort_projects,
    to_display_date,
    to_iso_date,
)


def _proj(title, **fields):
    return Project(fields=ProjectFields(title=title, **fields))


# ==================== Identity ====================


def test_key_is_the_local_id_and_never_a_remote_one():
    """Identity is local. A backend key is a mapping, not the identity."""
    p = _proj("ZTNA")
    p.link_remote("smartsheet", 123)
    assert p.key == p.id
    assert p.key != "123"
    assert p.remote_id("smartsheet") == "123"


def test_ids_are_unique():
    assert _proj("a").id != _proj("a").id


def test_remote_ids_are_stored_as_text():
    """An int row id and a string primary key are the same shape here."""
    p = _proj("x")
    p.link_remote("smartsheet", 4001)
    p.link_remote("d1", "abc-1")
    assert p.remote_id("smartsheet") == "4001"
    assert p.remote_id("d1") == "abc-1"


def test_unknown_backend_has_no_id():
    assert _proj("x").remote_id("nowhere") is None


def test_linking_twice_updates_rather_than_duplicates():
    p = _proj("x")
    p.link_remote("smartsheet", 1, modified_at="2026-08-01T00:00:00Z")
    p.link_remote("smartsheet", 2)
    assert p.remote_id("smartsheet") == "2"
    # A link that doesn't restate modified_at leaves it alone.
    assert p.remote["smartsheet"].modified_at == "2026-08-01T00:00:00Z"


# ==================== Merge bases ====================


def test_base_is_a_snapshot_not_a_reference():
    """A base that aliased `fields` would silently follow every later edit."""
    p = _proj("ZTNA", note="first")
    p.set_base("smartsheet")
    p.fields.note = "second"
    assert p.base_for("smartsheet").note == "first"
    assert p.fields.note == "second"


def test_no_base_until_synced():
    """None means "never synced", which is not the same as "unchanged"."""
    assert _proj("x").base_for("smartsheet") is None


# ==================== Per-field timestamps ====================


def test_touch_stamps_only_named_fields():
    p = _proj("x")
    p.touch("title", stamp="2026-08-12T10:00:00+00:00")
    assert p.changed_at("title") == "2026-08-12T10:00:00+00:00"
    assert p.changed_at("note") is None


def test_touch_defaults_to_now_and_is_tz_aware():
    """Naive stamps cannot be compared against another machine's."""
    p = _proj("x")
    p.touch("note")
    stamp = p.changed_at("note")
    assert stamp is not None and ("+" in stamp or stamp.endswith("Z"))


# ==================== Fields ====================


def test_starred_is_a_bool_not_sheet_text():
    assert _proj("a", starred=True).is_starred is True
    assert _proj("b").is_starred is False


def test_assigned_carries_emails():
    """The email is why an assignee survives a round trip to a contact column."""
    p = _proj("x", assigned=[Person(name="Ada Lovelace", email="al@example.edu")])
    assert p.assigned_names == ["Ada Lovelace"]
    assert p.fields.assigned[0].email == "al@example.edu"
    assert p.assigned_str == "Ada Lovelace"


def test_assigned_str_empty_when_unassigned():
    assert _proj("y").assigned_str == ""


def test_people_coerces_names_dicts_and_persons():
    coerced = people(
        ["Al", {"name": "Jeff", "email": "j@x.edu"}, Person(name="Sam")]
    )
    assert [(p.name, p.email) for p in coerced] == [
        ("Al", ""),
        ("Jeff", "j@x.edu"),
        ("Sam", ""),
    ]


def test_people_falls_back_to_the_email_as_a_name():
    assert people([{"email": "nobody@x.edu"}])[0].name == "nobody@x.edu"


def test_people_drops_blanks():
    assert people(["", "  ", None]) == []
    assert people(None) == []


def test_dates_are_stored_iso_and_shown_in_display_form():
    p = _proj("x", due_date="2026-12-30")
    assert p.due_date_iso == "2026-12-30"
    assert p.due_date == "12/30/2026"


def test_missing_date_renders_empty():
    assert _proj("x").due_date == ""
    assert _proj("x").due_date_iso == ""


def test_field_names_covers_every_canonical_field():
    """Merges, migrations, and timestamps all iterate this."""
    assert set(FIELD_NAMES) == set(ProjectFields.model_fields)


# ==================== Date conversion ====================


def test_to_display_date():
    assert to_display_date("2026-12-30") == "12/30/2026"
    assert to_display_date("2026-08-06T18:04:40Z") == "8/6/2026"
    assert to_display_date(None) == ""
    assert to_display_date("") == ""


def test_to_iso_date_accepts_typed_and_stored_formats():
    assert to_iso_date("12/30/2026") == "2026-12-30"
    assert to_iso_date("2026-12-30") == "2026-12-30"
    assert to_iso_date("") == ""  # clears the value


def test_to_iso_date_rejects_garbage():
    """None means "unparseable", so a typo is skipped rather than stored."""
    assert to_iso_date("next tuesday") is None
    assert to_iso_date("13/45/2026") is None


# ==================== Search and ordering ====================


def test_matches_searches_all_fields():
    p = _proj(
        "ZTNA",
        status="In progress",
        assigned=people(["Grace Hopper"]),
        note="Cloudflare PoC",
    )
    assert p.matches("ztna")
    assert p.matches("cloudflare")
    assert p.matches("hopper")
    assert p.matches("progress")
    assert not p.matches("nutanix")


def test_status_defaults_to_not_started():
    assert _proj("x").status == "Not started"


def test_sort_projects_order():
    ps = [
        _proj("done", status="Done"),
        _proj("prog", status="In progress"),
        _proj("star-ns", status="Not started", starred=True),
        _proj("hold", status="On Hold"),
        _proj("blocked", status="Blocked"),
        _proj("ns", status="Not started"),
    ]
    order = [p.title for p in sort_projects(ps)]
    # Starred first, then In progress, Not started, Blocked, On Hold, Done.
    assert order == ["star-ns", "prog", "ns", "blocked", "hold", "done"]


# ==================== Timestamp comparison ====================


def test_parse_stamp_always_returns_an_aware_datetime():
    """Mixing naive and aware datetimes in a subtraction raises TypeError.

    Both kinds legitimately exist here: stamps written now are aware, one
    carried over from the v2 store is not.
    """
    from projection.models import parse_stamp

    naive = parse_stamp("2026-08-11T22:39:33.762065")
    aware = parse_stamp("2026-08-11T22:39:33+00:00")
    assert naive is not None and naive.tzinfo is not None
    assert aware is not None and aware.tzinfo is not None
    # The whole point: these can be compared without blowing up.
    assert isinstance((naive - aware).total_seconds(), float)


def test_parse_stamp_reads_naive_input_as_local_time():
    """v2 wrote `datetime.now()` — local. Calling it UTC skews every age."""
    from datetime import datetime

    from projection.models import parse_stamp

    just_now = datetime.now().isoformat()  # naive, local, like v2's
    parsed = parse_stamp(just_now)
    assert parsed is not None
    age = (datetime.now().astimezone() - parsed).total_seconds()
    assert abs(age) < 5, "a naive local stamp should read as ~now, not offset"


def test_parse_stamp_rejects_junk():
    from projection.models import parse_stamp

    assert parse_stamp("not a time") is None
    assert parse_stamp(None) is None
    assert parse_stamp("") is None
