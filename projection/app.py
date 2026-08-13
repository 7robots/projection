"""Projection — a Textual TUI for projects, over a local-first store."""

from __future__ import annotations

from typing import Iterable

from textual.app import App, SystemCommand
from textual.screen import Screen
from textual.theme import Theme

from .backends import smartsheet_client
from .config import Config
from .panel import ProjectsPanel
from .screen import ProjectsScreen
from .smartsheet_api import SmartsheetClient

PROJECTION_THEME = Theme(
    name="projection",
    primary="#0A84FF",
    secondary="#5E5CE6",
    accent="#FF9F0A",
    warning="#FFD60A",
    error="#FF453A",
    success="#30D158",
    foreground="#F2F2F7",
    background="#1C1C1E",
    surface="#2C2C2E",
    panel="#3A3A3C",
    dark=True,
)


class ProjectsApp(App):
    """View and edit projects.

    A shell around `ProjectsScreen`: this class owns only what belongs to an
    application -- the theme, the keymap, and the command palette entries. The
    UI and its logic live in `ProjectsPanel`, which other apps can mount.
    """

    TITLE = "Projection"
    SUB_TITLE = "Projects"

    def __init__(
        self,
        client: SmartsheetClient | None = None,
        config: Config | None = None,
    ):
        super().__init__()
        # Read once here and handed down, so the panel and the app cannot end
        # up looking at two different reads of config.toml.
        self._config = config or Config.load()
        # One authenticated client, so the token is fetched from 1Password once.
        # Built through `smartsheet_client`, which applies the credential
        # config.toml names — a bare `SmartsheetClient()` cannot see it.
        self._client = client or smartsheet_client(self._config)

    def get_default_screen(self) -> ProjectsScreen:
        # The projects view *is* this app, so it is the default screen rather
        # than something pushed on top: screen_stack stays one deep until a
        # modal opens.
        return ProjectsScreen(self._client, config=self._config)

    def on_mount(self) -> None:
        self.register_theme(PROJECTION_THEME)
        self.theme = "projection"
        if self._config.keys.overrides:
            self.set_keymap(dict(self._config.keys.overrides))

    async def on_unmount(self) -> None:
        """The app owns the client, so the app closes it."""
        await self._client.aclose()

    @property
    def panel(self) -> ProjectsPanel:
        """The projects panel of the default screen."""
        return self.query_one("#projects-panel", ProjectsPanel)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "New project", "Create a new project", self.panel.action_new_project
        )
        yield SystemCommand(
            "Refresh projects", "Reload from the backend", self.panel.action_refresh
        )
        # One entry per configured hook, so a hook with no key is still reachable.
        for hook in self._config.hooks:
            yield SystemCommand(
                f"Run: {hook.display}",
                f"Hook over {hook.input} projects"
                + (" (with review)" if hook.wants_review else ""),
                _hook_runner(self.panel, hook.id),
            )
        yield SystemCommand(
            "Projects backend",
            "Choose, create, or connect the backend to sync with",
            self.panel.action_setup,
        )
        yield SystemCommand(
            "Keyboard reference", "Show the key bindings", self.panel.action_help
        )


def main():
    """Entry point for the TUI application."""
    app = ProjectsApp()
    app.run()


if __name__ == "__main__":
    main()


def _hook_runner(panel: ProjectsPanel, hook_id: str):
    """A no-argument callable for the palette, bound to one hook.

    A closure over the loop variable would give every entry the last hook.
    """

    def run() -> None:
        panel.run_worker(panel.action_run_hook(hook_id), name=f"hook-{hook_id}")

    return run
