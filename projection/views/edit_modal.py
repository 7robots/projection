"""Modal dialog for editing projects."""

import os
import subprocess
import tempfile
from typing import Optional

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import (
    Static,
    Button,
    Footer,
    TextArea,
    Input,
    Select,
    SelectionList,
    LoadingIndicator,
)
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.binding import Binding

from ..models import Project
from ..models import to_iso_date


class EditResult:
    """Result returned from the edit modal."""

    def __init__(
        self,
        title: str,
        status: str,
        assigned: list[str],
        due_date: str,
        note: str,
        starred: bool = False,
        is_new: bool = False,
        key: str | None = None,
        completed: bool = False,
    ):
        self.title = title
        self.status = status
        self.assigned = assigned
        self.due_date = due_date
        self.note = note
        self.starred = starred
        self.is_new = is_new
        self.key = key  # id of the edited project (None when new)
        self.completed = completed


class EditModal(ModalScreen[EditResult | None]):
    """Modal screen for editing or creating a project."""

    # priority=True on all four: an Input or TextArea with focus binds several
    # of these itself (ctrl+e, ctrl+d, ctrl+k...), and a focused widget is
    # checked before the screen -- so without priority the dialog's own
    # shortcuts stop working the moment the cursor is in a field, which is the
    # whole time. It also keeps the Footer honest, since it lists the bindings
    # that are actually reachable.
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+e", "open_in_editor", "Editor", priority=True),
        Binding("ctrl+d", "toggle_complete", "Done", priority=True),
    ]

    DEFAULT_CSS = """
    EditModal {
        align: center middle;
    }

    EditModal > Container {
        width: 80%;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    EditModal .modal-title {
        dock: top;
        height: 3;
        padding: 1;
        text-style: bold;
        text-align: center;
        background: $primary;
        color: $text;
    }

    EditModal .form-container {
        padding: 1;
        height: 1fr;
    }

    EditModal .field-row {
        height: auto;
        margin-bottom: 1;
    }

    EditModal .field-label {
        width: 12;
        height: 3;
        padding: 1 0;
    }

    EditModal .field-input {
        width: 1fr;
    }

    EditModal Input {
        width: 100%;
    }

    EditModal Select {
        width: 100%;
    }

    EditModal #note-text {
        height: 8;
        margin-top: 1;
    }

    EditModal #assigned-select {
        width: 1fr;
        height: auto;
        max-height: 6;
        border: tall $panel;
        background: $surface;
    }

    /* Not docked: the Footer below owns the bottom row. Two widgets docked
       bottom overlap, which clipped the buttons' last line. */
    EditModal .button-row {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    EditModal Button {
        margin-left: 2;
        min-width: 12;
    }

    /* Inside the dialog, not at the bottom of the terminal: the hints belong
       to this dialog, and it is not full-screen. */
    EditModal Footer {
        height: 1;
        background: $panel;
    }
    """

    def __init__(
        self,
        project: Optional[Project] = None,
        status_options: Optional[list[str]] = None,
        contact_options: Optional[list[str]] = None,
    ):
        """Initialize the edit modal.

        Args:
            project: Project to edit, or None to create a new project
            status_options: Status values the sheet's picklist accepts
            contact_options: Assignable contact names from the sheet's
                Assigned To column
        """
        super().__init__()
        self.project = project
        self.is_create_mode = project is None
        self.status_options = status_options or ["Not started", "In progress", "Done"]
        self.key = project.key if project else None
        # Offer every known contact, plus any already on this row that are no
        # longer column options (so editing doesn't silently drop them).
        assigned = list(project.assigned_names) if project else []
        options = list(contact_options or [])
        options += [name for name in assigned if name not in options]
        self.contact_options = options
        self.assigned = assigned

    def compose(self) -> ComposeResult:
        title = "Create New Project" if self.is_create_mode else "Edit Project"

        # Get current values or defaults
        current_title = self.project.title if self.project else ""
        current_status = self.project.status if self.project else self.status_options[0]
        current_due_date = self.project.due_date if self.project else ""
        current_note = self.project.note_text if self.project else ""
        # The Select shows Yes/No; the model stores a bool.
        current_starred = "Yes" if (self.project and self.project.is_starred) else "No"

        # Build status options for Select widget; keep an off-list status
        # from the sheet selectable rather than crashing the Select.
        status_options = [(s, s) for s in self.status_options]
        if current_status not in self.status_options:
            status_options.insert(0, (current_status, current_status))

        with Container():
            yield Static(title, classes="modal-title")

            # Scrolls: the button row and Footer are docked, and a form taller
            # than the dialog used to render *under* them -- the last field sat
            # behind the buttons.
            with VerticalScroll(classes="form-container"):
                # Title field
                with Horizontal(classes="field-row"):
                    yield Static("Title:", classes="field-label")
                    yield Input(
                        value=current_title,
                        placeholder="Project title",
                        id="title-input",
                        classes="field-input",
                    )

                # Status field
                with Horizontal(classes="field-row"):
                    yield Static("Status:", classes="field-label")
                    yield Select(
                        status_options,
                        value=current_status,
                        id="status-select",
                        classes="field-input",
                    )

                # Assigned field — a multi-select over the sheet's contacts.
                with Horizontal(classes="field-row"):
                    yield Static("Assigned:", classes="field-label")
                    if self.contact_options:
                        yield SelectionList[str](
                            *(
                                (name, name, name in self.assigned)
                                for name in self.contact_options
                            ),
                            id="assigned-select",
                        )
                    else:
                        # Contacts load with the sheet; if that hasn't happened
                        # yet, fall back to free text rather than blocking edits.
                        yield Input(
                            value=", ".join(self.assigned),
                            placeholder="Comma-separated names",
                            id="assigned-input",
                            classes="field-input",
                        )

                # Due date field
                with Horizontal(classes="field-row"):
                    yield Static("Due Date:", classes="field-label")
                    yield Input(
                        value=current_due_date,
                        placeholder="M/D/YYYY (e.g. 2/10/2026)",
                        id="due-date-input",
                        classes="field-input",
                    )

                # Sync-to-exec-summary field
                with Horizontal(classes="field-row"):
                    yield Static("Sync:", classes="field-label")
                    yield Select(
                        [("No", "No"), ("Yes", "Yes")],
                        value=current_starred,
                        id="starred-select",
                        classes="field-input",
                    )

                # Update text area
                yield Static("Status Update:")
                yield TextArea(
                    current_note,
                    id="note-text",
                )

            # Secondary actions first, then Cancel, then the primary -- the
            # shape every dialog in both apps uses. Shortcuts are not repeated
            # in the labels; the Footer below derives them from BINDINGS, so
            # they cannot drift.
            with Horizontal(classes="button-row"):
                yield Button("Editor", variant="default", id="btn-editor")
                yield Button("Done", variant="success", id="btn-done")
                yield Button("Cancel", variant="default", id="btn-cancel")
                yield Button("Save", variant="primary", id="btn-save")
            yield Footer()

    def on_mount(self) -> None:
        # Land in the first field, as remtui's form does -- typing should edit
        # the thing you opened the dialog for, not move button focus.
        self.query_one("#title-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-save":
            self.action_save()
        elif event.button.id == "btn-done":
            self.action_toggle_complete()
        elif event.button.id == "btn-editor":
            self.action_open_in_editor()
        elif event.button.id == "btn-cancel":
            self.action_cancel()

    def _selected_assignees(self) -> list[str]:
        """Assignees from whichever assigned widget is mounted.

        SelectionList reports its selection in the order the user toggled
        names; normalize to the displayed order so re-saving an unchanged
        row doesn't rewrite the cell in a different sequence.
        """
        selection = self.query("#assigned-select")
        if selection:
            chosen = set(selection.only_one(SelectionList).selected)
            return [name for name in self.contact_options if name in chosen]
        value = self.query_one("#assigned-input", Input).value
        return [name.strip() for name in value.split(",") if name.strip()]

    def _get_form_data(self) -> EditResult:
        """Get all form data as an EditResult."""
        title_input = self.query_one("#title-input", Input)
        status_select = self.query_one("#status-select", Select)
        due_date_input = self.query_one("#due-date-input", Input)
        starred_select = self.query_one("#starred-select", Select)
        note_text = self.query_one("#note-text", TextArea)

        return EditResult(
            title=title_input.value.strip(),
            status=str(status_select.value) if status_select.value else self.status_options[0],
            assigned=self._selected_assignees(),
            due_date=due_date_input.value.strip(),
            note=note_text.text,
            starred=str(starred_select.value) == "Yes",
            is_new=self.is_create_mode,
            key=self.key,
        )

    def _validate(self, result: EditResult) -> bool:
        """Validate the form; notify and return False if invalid."""
        if not result.title:
            self.notify("Title is required", severity="error")
            return False
        # Catch a bad date here: Smartsheet would reject it, and the local
        # cache would show the typo until a refresh silently reverted it.
        if result.due_date and to_iso_date(result.due_date) is None:
            self.notify(
                f"{result.due_date!r} isn't a date — use M/D/YYYY",
                severity="error",
            )
            return False
        return True

    def action_save(self) -> None:
        """Save the project and dismiss the modal."""
        result = self._get_form_data()
        if not self._validate(result):
            return
        self.dismiss(result)

    def action_cancel(self) -> None:
        """Cancel and dismiss the modal."""
        self.dismiss(None)

    def action_toggle_complete(self) -> None:
        """Mark task as completed and dismiss."""
        result = self._get_form_data()
        if not self._validate(result):
            return
        result.completed = True
        self.dismiss(result)

    def action_open_in_editor(self) -> None:
        """Open the update text in an external editor (vim by default)."""
        text_area = self.query_one("#note-text", TextArea)
        current_text = text_area.text

        # Create temp file with current content
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            delete=False,
        ) as f:
            f.write(current_text)
            temp_path = f.name

        try:
            editor = os.environ.get("EDITOR", "vim")

            # Suspend the app and run the editor
            with self.app.suspend():
                subprocess.run([editor, temp_path], check=True)

            # Read back the edited content
            with open(temp_path) as f:
                edited_text = f.read()

            # Update the TextArea with the edited content
            text_area.clear()
            text_area.insert(edited_text)

        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)


class LoadingModal(ModalScreen[None]):
    """A non-dismissable popup shown while a background task runs.

    Push it before awaiting the work, then call `dismiss()` when done.
    """

    DEFAULT_CSS = """
    LoadingModal {
        align: center middle;
    }

    LoadingModal > Container {
        width: 56;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
        align: center middle;
    }

    LoadingModal .loading-message {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    LoadingModal LoadingIndicator {
        height: 1;
    }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container():
            yield Static(self.message, classes="loading-message")
            yield LoadingIndicator()


class ConfirmDeleteModal(ModalScreen[bool]):
    """Modal to confirm project deletion."""

    BINDINGS = [
        Binding("escape,n", "cancel", "Cancel"),
        Binding("y", "confirm", "Delete"),
    ]

    DEFAULT_CSS = """
    ConfirmDeleteModal {
        align: center middle;
    }

    ConfirmDeleteModal > Container {
        width: 60;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }

    ConfirmDeleteModal .modal-title {
        dock: top;
        height: 3;
        padding: 1;
        text-style: bold;
        text-align: center;
        background: $error;
        color: $text;
    }

    ConfirmDeleteModal .message {
        padding: 2;
        text-align: center;
    }

    ConfirmDeleteModal .button-row {
        height: 3;
        align-horizontal: right;
    }

    ConfirmDeleteModal Footer {
        height: 1;
        background: $panel;
    }

    ConfirmDeleteModal Button {
        margin-left: 2;
        min-width: 12;
    }
    """

    def __init__(self, project: Project):
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        with Container():
            yield Static("Delete Project", classes="modal-title")
            yield Static(
                f"Are you sure you want to delete:\n\n[bold]{self.project.title}[/bold]",
                classes="message"
            )
            with Horizontal(classes="button-row"):
                yield Button("Cancel", variant="default", id="btn-cancel")
                yield Button("Delete", variant="error", id="btn-delete")
            yield Footer()

    def on_mount(self) -> None:
        # Focus the safe option: a stray Enter right after pressing "d"
        # must not delete irreversibly.
        self.query_one("#btn-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-delete":
            self.dismiss(True)
        else:
            self.dismiss(False)
