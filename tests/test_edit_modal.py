"""Tests for the edit modal, especially the Assigned To picker."""

from textual.app import App, ComposeResult
from textual.widgets import Input, SelectionList

from projection.models import Project, ProjectFields, people
from projection.views import EditModal

CONTACTS = ["Ada Lovelace", "Grace Hopper"]


class Host(App):
    """Minimal app that just hosts the modal under test."""

    def __init__(self, modal: EditModal):
        super().__init__()
        self._modal = modal
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return iter(())

    async def on_mount(self) -> None:
        self.push_screen(self._modal, lambda r: setattr(self, "result", r))


PROJECT_ID = "proj-42"


def _project(**fields):
    if "assigned" in fields:
        fields["assigned"] = people(fields["assigned"])
    project = Project(id=PROJECT_ID, fields=ProjectFields(title="ZTNA", **fields))
    project.link_remote("smartsheet", 42)
    return project


async def test_assigned_prechecks_current_assignees():
    modal = EditModal(
        _project(status="In progress", assigned=["Grace Hopper"]),
        status_options=["Not started", "In progress", "Done"],
        contact_options=CONTACTS,
    )
    async with Host(modal).run_test() as pilot:
        await pilot.pause()
        selection = modal.query_one("#assigned-select", SelectionList)
        assert list(selection.selected) == ["Grace Hopper"]


async def test_save_returns_selected_assignees_and_key():
    modal = EditModal(
        _project(status="In progress", assigned=["Grace Hopper"]),
        status_options=["Not started", "In progress", "Done"],
        contact_options=CONTACTS,
    )
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        selection = modal.query_one("#assigned-select", SelectionList)
        # Toggle in reverse display order to prove the result is normalized.
        selection.deselect_all()
        selection.select("Grace Hopper")
        selection.select("Ada Lovelace")
        modal.action_save()
        await pilot.pause()

    assert app.result.assigned == CONTACTS  # displayed order, not click order
    assert app.result.key == PROJECT_ID
    assert app.result.is_new is False


async def test_deselecting_everyone_returns_empty_list():
    modal = EditModal(
        _project(assigned=["Grace Hopper"]),
        contact_options=CONTACTS,
    )
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#assigned-select", SelectionList).deselect_all()
        modal.action_save()
        await pilot.pause()

    assert app.result.assigned == []


async def test_assignee_not_in_contact_options_is_kept():
    """A name already on the row must not be silently dropped by an edit."""
    modal = EditModal(
        _project(assigned=["Former Colleague"]),
        contact_options=CONTACTS,
    )
    async with Host(modal).run_test() as pilot:
        await pilot.pause()
        selection = modal.query_one("#assigned-select", SelectionList)
        assert "Former Colleague" in list(selection.selected)


async def test_falls_back_to_text_input_without_contacts():
    """Contacts load with the sheet; before that, editing still works."""
    modal = EditModal(_project(assigned=["Grace Hopper"]), contact_options=[])
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The row's own assignee still seeds the options, so we get a list.
        assert modal.query("#assigned-select")

    modal = EditModal(project=None, contact_options=[])
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#assigned-input", Input).value = "Ada Lovelace, Grace Hopper"
        modal.query_one("#title-input", Input).value = "New"
        modal.action_save()
        await pilot.pause()

    assert app.result.assigned == CONTACTS


async def test_ctrl_d_marks_completed():
    modal = EditModal(_project(status="In progress"), contact_options=CONTACTS)
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.action_toggle_complete()
        await pilot.pause()

    assert app.result.completed is True


async def test_unparseable_due_date_is_rejected():
    """Otherwise the typo shows locally until a refresh silently reverts it."""
    modal = EditModal(_project(), contact_options=CONTACTS)
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#due-date-input", Input).value = "next tuesday"
        modal.action_save()
        await pilot.pause()
        assert app.result == "unset"  # still open, nothing saved


async def test_valid_due_date_saves():
    modal = EditModal(_project(), contact_options=CONTACTS)
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#due-date-input", Input).value = "3/15/2027"
        modal.action_save()
        await pilot.pause()
    assert app.result.due_date == "3/15/2027"


async def test_empty_due_date_saves():
    modal = EditModal(_project(), contact_options=CONTACTS)
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#due-date-input", Input).value = ""
        modal.action_save()
        await pilot.pause()
    assert app.result.due_date == ""


async def test_empty_title_is_rejected():
    modal = EditModal(project=None, contact_options=CONTACTS)
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.action_save()
        await pilot.pause()
        # Still open, nothing dismissed.
        assert app.result == "unset"


async def test_duplicate_titles_are_allowed():
    """Row id is identity now, so a repeated title must save fine."""
    modal = EditModal(project=None, contact_options=CONTACTS)
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#title-input", Input).value = "ZTNA"
        modal.action_save()
        await pilot.pause()

    assert app.result.title == "ZTNA"
    assert app.result.is_new is True
    assert app.result.starred is False
