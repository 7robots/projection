"""Keyboard reference modal."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

HELP_TEXT = """\
[bold $accent]Navigate[/]
  j / ↓, k / ↑        move down / up
  ← / h, → / l        focus sidebar / projects
  tab                 switch pane
  g, G                jump to top / bottom
  pgup / pgdn         page (selection follows)

[bold $accent]Projects[/]
  n / a               new project
  e / enter           edit selected (full update text)
  space               toggle done
  s                   star / unstar ⟳
  c                   resolve conflicting changes ⚠
  d / ⌫               delete (y confirms, n cancels)

[bold $accent]Views[/]
  /                   filter current view
  esc                 clear filter
  r                   refresh from the backend

[bold $accent]App[/]
  ,                   backend setup (create / connect / local only)
  ctrl+p              command palette (actions, hooks & themes)
  ?                   this help
  q                   quit

[bold $accent]Hooks[/]  [dim]config.toml: \\[[hooks]] -- your own scripts, on your own keys[/]
  (configured)        run a hook over all / starred / selected projects

[bold $accent]Vim profile[/]  [dim]config.toml: \\[keys] profile = "vim"[/]
  gg / G              top / bottom (g becomes a prefix)
  ctrl+d / ctrl+u     half page down / up
  ctrl+f / ctrl+b     page down / up
  :                   command palette
  o                   new project
"""


class HelpScreen(ModalScreen[None]):
    # Carries its own styles so the dialog looks right wherever the panel is
    # hosted -- these used to live in the app-level stylesheet.
    DEFAULT_CSS = """
HelpScreen {
    align: center middle;
}

.help-dialog {
    width: 60;
    height: auto;
    max-height: 90%;
    background: $surface;
    border: round $accent;
    padding: 1 2;
}

#help-title {
    text-style: bold;
    color: $text;
    padding-bottom: 1;
}

#help-footer {
    margin-top: 1;
    text-align: center;
}
    """

    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="help-dialog"):
            yield Static("⌨  Keyboard Reference", id="help-title")
            yield Static(HELP_TEXT, id="help-body")
            yield Static("[dim]esc to close[/]", id="help-footer")

    def action_close(self) -> None:
        self.dismiss(None)
