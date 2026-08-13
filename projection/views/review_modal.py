"""Review a hook's draft before letting it commit.

A hook in `mode = "review"` runs in two phases, and this sits between them: the
draft comes back from the script, is shown here for editing, and only an explicit
approval triggers the second call. Cancelling means the commit phase never
happens at all.

That split is the point. Whatever irreversible thing the script does — writing to
a shared sheet, filing a ticket, sending a message — stays in the script, while
the moment a human can see it and say no stays in the TUI.

Note what this does *not* protect against: if the script fed the projects to an
LLM to produce the draft, any tool call it made has already happened by the time a
character appears here. Reviewing gates the *commit*, not the drafting. See
`hooks.py` for where that containment actually lives.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Static, TextArea


class ReviewModal(ModalScreen[str | None]):
    """Edit and approve a hook's draft. Dismisses with the text, or None."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "confirm", "Approve"),
        Binding("ctrl+e", "open_in_editor", "Open in $EDITOR"),
    ]

    DEFAULT_CSS = """
    ReviewModal {
        align: center middle;
    }

    ReviewModal > Container {
        width: 90%;
        height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    ReviewModal .modal-title {
        dock: top;
        height: 3;
        padding: 1;
        text-style: bold;
        text-align: center;
        background: $primary;
        color: $text;
    }

    ReviewModal #review-text {
        height: 1fr;
        margin: 1 0;
    }

    ReviewModal .button-row {
        height: 3;
        align-horizontal: right;
    }

    ReviewModal Footer {
        height: 1;
        background: $panel;
    }

    ReviewModal Button {
        margin-left: 2;
        min-width: 12;
    }
    """

    def __init__(
        self,
        draft: str,
        *,
        title: str = "Review",
        approve_label: str = "Approve",
    ) -> None:
        super().__init__()
        self.draft = draft
        self.title_text = title
        self.approve_label = approve_label

    def compose(self) -> ComposeResult:
        with Container():
            yield Static(self.title_text, classes="modal-title")
            yield TextArea(self.draft, id="review-text")
            with Horizontal(classes="button-row"):
                yield Button("Editor", variant="default", id="btn-editor")
                yield Button("Cancel", variant="default", id="btn-cancel")
                yield Button(self.approve_label, variant="primary", id="btn-save")
            yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_confirm()
        elif event.button.id == "btn-editor":
            self.action_open_in_editor()
        elif event.button.id == "btn-cancel":
            self.action_cancel()

    def action_confirm(self) -> None:
        text = self.query_one("#review-text", TextArea).text.strip()
        if not text:
            self.notify("Nothing to approve — the draft is empty", severity="error")
            return
        self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_open_in_editor(self) -> None:
        text_area = self.query_one("#review-text", TextArea)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(text_area.text)
            temp_path = f.name
        try:
            editor = os.environ.get("EDITOR", "vim")
            with self.app.suspend():
                subprocess.run([editor, temp_path], check=True)
            with open(temp_path) as f:
                edited = f.read()
            text_area.clear()
            text_area.insert(edited)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
