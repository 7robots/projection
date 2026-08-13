"""Resolving a conflict: choose, per field, whose value wins.

Reached with `c`. Only appears for a project where both sides changed the same
field to different values since the last sync — everything else the merge settles
on its own.

Nothing is decided for the user here, and nothing is decided *by* opening it:
cancelling leaves every conflict exactly as it was.
"""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, RadioButton, RadioSet, Static

from ..models import Project, field_value_text

# What the two sides are called in the UI. "Theirs" is deliberately vague about
# who: on a shared sheet it may be a colleague, on another device it is you.
MINE = "mine"
THEIRS = "theirs"

# Field name -> the label shown for it.
_LABELS = {
    "title": "Title",
    "status": "Status",
    "assigned": "Assigned",
    "due_date": "Due date",
    "note": "Note",
    "starred": "Starred",
}


class ConflictModal(ModalScreen[Optional[dict[str, bool]]]):
    """Per-field chooser for one project's conflicts.

    Dismisses with `{field_name: take_theirs}` for every field, or None when
    cancelled.
    """

    # priority=True for the same reason as the edit modal: a focused RadioSet
    # binds arrow keys and space itself, and a focused widget is checked before
    # the screen, so without priority the dialog's own shortcuts stop working the
    # moment the cursor lands in a control -- which is immediately.
    BINDINGS = [
        Binding("ctrl+s", "save", "Apply", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+left", "all_mine", "Keep all mine", priority=True),
        Binding("ctrl+right", "all_theirs", "Take all theirs", priority=True),
    ]

    DEFAULT_CSS = """
    ConflictModal {
        align: center middle;
    }

    ConflictModal > Container {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: thick $warning;
        background: $surface;
    }

    ConflictModal .modal-title {
        width: 100%;
        padding: 1 2 0 2;
        text-style: bold;
        color: $warning;
    }

    ConflictModal .modal-hint {
        width: 100%;
        padding: 0 2 1 2;
        color: $text-muted;
    }

    ConflictModal .fields {
        height: auto;
        max-height: 24;
        padding: 0 2;
    }

    ConflictModal .field-name {
        text-style: bold;
        padding-top: 1;
    }

    ConflictModal .base-note {
        color: $text-muted;
        padding-bottom: 1;
    }

    ConflictModal RadioSet {
        width: 100%;
        height: auto;
        border: none;
        background: transparent;
    }

    ConflictModal .buttons {
        width: 100%;
        height: auto;
        align-horizontal: right;
        padding: 1 2;
    }

    ConflictModal .buttons Button {
        margin-left: 1;
    }

    ConflictModal Footer {
        height: 1;
        background: $panel;
    }
    """

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.fields = project.conflict_fields()

    def compose(self) -> ComposeResult:
        with Container():
            yield Static(
                f"Conflicting changes — {self.project.title}",
                classes="modal-title",
            )
            yield Static(
                "Both sides changed these fields since the last sync. "
                "Nothing has been pushed for them yet.",
                classes="modal-hint",
            )
            with VerticalScroll(classes="fields"):
                for name in self.fields:
                    conflict = self.project.conflicts[name]
                    yield Static(
                        _LABELS.get(name, name), classes="field-name"
                    )
                    with RadioSet(id=f"choice-{name}"):
                        yield RadioButton(
                            f"Mine: {field_value_text(name, conflict.mine)}",
                            value=True,
                            id=f"{name}-mine",
                        )
                        yield RadioButton(
                            f"Theirs: {field_value_text(name, conflict.theirs)}",
                            id=f"{name}-theirs",
                        )
                    # The shared ancestor, so "what did I actually change?" is
                    # answerable without leaving the dialog.
                    yield Static(
                        f"was: {field_value_text(name, conflict.base)}",
                        classes="base-note",
                    )
            with Horizontal(classes="buttons"):
                yield Button("Keep all mine", id="btn-all-mine")
                yield Button("Take all theirs", id="btn-all-theirs")
                yield Button("Cancel", id="btn-cancel")
                yield Button("Apply", variant="primary", id="btn-save")
            yield Footer()

    def on_mount(self) -> None:
        # Focus the first chooser, matching the edit dialog's "cursor starts in
        # the first control" contract.
        if self.fields:
            self.query_one(f"#choice-{self.fields[0]}", RadioSet).focus()

    # -- choices ------------------------------------------------------------

    def _set_all(self, take_theirs: bool) -> None:
        for name in self.fields:
            radio_set = self.query_one(f"#choice-{name}", RadioSet)
            radio_set._nodes[1 if take_theirs else 0].value = True

    def _selection(self) -> dict[str, bool]:
        """Field -> whether their value was chosen."""
        chosen: dict[str, bool] = {}
        for name in self.fields:
            radio_set = self.query_one(f"#choice-{name}", RadioSet)
            index = radio_set.pressed_index
            chosen[name] = index == 1
        return chosen

    def action_all_mine(self) -> None:
        self._set_all(False)

    def action_all_theirs(self) -> None:
        self._set_all(True)

    def action_save(self) -> None:
        self.dismiss(self._selection())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        elif event.button.id == "btn-cancel":
            self.action_cancel()
        elif event.button.id == "btn-all-mine":
            self.action_all_mine()
        elif event.button.id == "btn-all-theirs":
            self.action_all_theirs()
