"""Projection's own screen: the panel, framed by a header and footer."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen

from textual.widgets import Footer, Header

from .config import Config
from .panel import ProjectsPanel
from .smartsheet_api import SmartsheetClient


class ProjectsScreen(Screen[None]):
    """Full-screen projects view.

    Thin by design: everything that does the work lives in `ProjectsPanel`, so
    another app can mount the panel without inheriting a header, a footer, or a
    theme.
    """

    def __init__(
        self,
        client: SmartsheetClient,
        *,
        config: Config | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client = client
        self._config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ProjectsPanel(self._client, config=self._config, id="projects-panel")
        yield Footer()

    @property
    def panel(self) -> ProjectsPanel:
        return self.query_one("#projects-panel", ProjectsPanel)
