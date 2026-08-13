"""The projects panel: Projection's UI as an embeddable widget.

A sidebar of smart lists (one per project status, plus All / Starred) beside the
project list for the selected view. All views read from the local store, which is
the source of record; a backend is reconciled with it in the background.
"""

import asyncio
import dataclasses
import time
from typing import Iterable

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, NoBinding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, ListView, OptionList, Static

from .models import Project, sort_projects
from .sync import SyncCoordinator, SyncEvent
from .config import (
    DONE_STATUS,
    Config,
    status_color,
    status_icon,
)
from . import hooks, secrets
from .backends import (
    Backend,
    BackendError,
    ProbeResult,
    build_backend,
    smartsheet_client,
)
from .smartsheet_api import SmartsheetClient
from .views import (
    ColumnMapModal,
    ConflictModal,
    EditModal,
    ConfirmDeleteModal,
    EditResult,
    HelpScreen,
    LoadingModal,
    ReviewModal,
    SetupChoice,
    SetupModal,
)
from .widgets import (
    COLOR_OVERDUE,
    DONE_STATUSES,
    SMART_VIEWS,
    TRANSIENT_VIEWS,
    ProjectItem,
    ProjectList,
    ViewHeader,
    humanize_age,
    logo,
    nav_header,
    smart_option,
    status_option,
    sync_age_seconds,
)

# Show a dim "synced Xh ago" note once the cache is older than this.
_STALE_AFTER_SECONDS = 3600.0


_SMART_BY_KEY = {view.key: view for view in SMART_VIEWS}


class ProjectsPanel(Vertical):
    """The projects UI: sidebar of views and statuses, plus the project list.

    A widget rather than a Screen or an App, so it can be composed anywhere --
    Projection's own screen wraps it in a Header and Footer, and librarian mounts
    it in a modal over its right-hand panels. Styles live in DEFAULT_CSS, which
    Textual scopes to this widget, so hosting it cannot restyle the host.
    """

    DEFAULT_CSS = """
/* The panel fills whatever hosts it -- see the note in remtui's panel: an
   auto height plus a 1fr child makes on_resize oscillate. */
ProjectsPanel {
    width: 100%;
    height: 1fr;
}

/* ── layout ─────────────────────────────────────────────────────────── */

#body {
    height: 1fr;
}

#sidebar {
    width: 26;
    min-width: 20;
    max-width: 34;
    background: $surface;
    border-right: vkey $panel;
    padding: 1 0 0 0;
}

#logo {
    height: auto;
    padding: 0 0 1 0;
    text-align: center;
}

#nav {
    background: transparent;
    border: none;
    padding: 0 1;
    scrollbar-size-vertical: 1;
}

#nav:focus {
    border: none;
    background: transparent;
}

#main {
    background: $background;
}

/* ── project list ───────────────────────────────────────────────────── */

#projects {
    background: transparent;
    padding: 0 1 1 1;
    scrollbar-size-vertical: 1;
}

ProjectItem {
    layout: horizontal;
    height: auto;
    padding: 0 1;
    margin-bottom: 1;
}

/* The app stylesheet overrides ListView's built-in cursor styling, so the
   selection states are (re)defined here: a visible gray bar when the pane
   is unfocused, a primary tint when it has focus. */
#projects > ProjectItem.-highlight {
    background: $panel;
}

#projects:focus > ProjectItem.-highlight {
    background: $primary 35%;
}

#projects > ProjectItem.-hovered {
    background: $boost;
}

ProjectItem .check {
    width: 3;
}

ProjectItem .body {
    width: 1fr;
    height: auto;
}

ProjectItem .meta {
    margin-left: 2;
}

ProjectItem.-done {
    opacity: 55%;
}

#filter {
    display: none;
    margin: 0 2;
    border: tall $primary;
}

#filter.-visible {
    display: block;
}

#empty {
    display: none;
    height: 1fr;
    content-align: center middle;
    color: $text-muted;
    text-style: italic;
}

#empty.-visible {
    display: block;
}

/* ── help modal ─────────────────────────────────────────────────────── */
    """

    BINDINGS = [
        Binding("n,a", "new_project", "New", id="project.new"),
        Binding("e", "edit", "Edit", id="project.edit"),
        Binding("space", "toggle_done", "Done", id="project.done"),
        Binding("d,delete,backspace", "delete", "Delete", id="project.delete"),
        Binding(
            "s",
            "toggle_star",
            "Star",
            show=False,
            # Starring is generic: it is Projection's "pin this" flag. The
            # exec-summary script happens to read it as "include me", which is
            # the script's business, not the core's.
            tooltip="Star this project ⟳",
            id="project.star",
        ),
        Binding(
            "c",
            "resolve_conflicts",
            "Conflicts",
            show=False,
            tooltip="Resolve conflicting changes on this project",
            id="project.conflicts",
        ),
        Binding("r", "refresh", "Refresh", show=False, id="view.refresh"),
        Binding(
            "comma",
            "setup",
            "Setup",
            show=False,
            tooltip="Choose, create, or connect a projects backend",
            id="app.setup",
        ),
        Binding("slash", "show_filter", "Filter", id="view.filter"),
        Binding("escape", "dismiss_filter", show=False, id="view.dismiss-filter"),
        Binding("j", "vim_down", show=False, id="nav.down"),
        Binding("k", "vim_up", show=False, id="nav.up"),
        Binding("left,h", "focus_nav", "Lists", show=False, id="nav.left"),
        Binding("right,l", "focus_projects", "Projects", show=False, id="nav.right"),
        # priority so it beats the Screen's built-in tab → focus_next binding
        Binding(
            "tab", "toggle_pane", "Switch pane", priority=True, id="nav.switch-pane"
        ),
        Binding("g", "go_top", show=False, id="nav.top"),
        Binding("G", "go_bottom", show=False, id="nav.bottom"),
        Binding("question_mark", "help", "Help", id="app.help"),
        # `app.quit`, not `quit`: a widget binding's action resolves against
        # the widget, and the panel has no action_quit, so a bare `quit` here
        # silently does nothing. Namespacing it targets the app that hosts the
        # panel -- which a host can override with a priority binding of its own.
        Binding("q", "app.quit", "Quit", id="app.quit"),
        # vim profile extras — inert unless [keys] profile = "vim"
        Binding("ctrl+d", "half_page_down", "½ page down", show=False, id="vim.half-down"),
        Binding("ctrl+u", "half_page_up", "½ page up", show=False, id="vim.half-up"),
        Binding("ctrl+f", "cursor_page_down", "Page down", show=False, id="vim.page-down"),
        Binding("ctrl+b", "cursor_page_up", "Page up", show=False, id="vim.page-up"),
        Binding("colon", "vim_palette", "Palette", show=False, id="vim.palette"),
        Binding("o", "vim_new", "New", show=False, id="vim.new"),
    ]

    # Actions that only exist in the vim key profile.
    _VIM_ACTIONS = frozenset(
        {"half_page_down", "half_page_up", "cursor_page_down", "cursor_page_up",
         "vim_palette", "vim_new"}
    )
    # Actions that need a selected project (grayed in the footer without one).
    _SELECTION_ACTIONS = frozenset(
        {"edit", "delete", "toggle_done", "toggle_star", "resolve_conflicts"}
    )
    # Actions that must not fire while a modal is open (app bindings stay
    # live under modal screens for any key the modal doesn't consume).
    _MAIN_SCREEN_ACTIONS = _VIM_ACTIONS | _SELECTION_ACTIONS | frozenset(
        {"new_project", "refresh", "run_hook", "show_filter", "toggle_pane",
         "go_top", "go_bottom", "focus_nav", "focus_projects", "setup"}
    )

    # Seconds within which a second `g` completes the gg chord (vim profile).
    _GG_CHORD_SECONDS = 0.75

    def __init__(
        self,
        client: SmartsheetClient | None = None,
        *,
        config: Config | None = None,
        show_logo: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # The wordmark identifies the app when it *is* the app. Embedded in
        # another TUI the host already says what this is, so three rows of
        # sidebar are better spent on the lists.
        self._show_logo = show_logo
        # A host that embeds this panel has no reason to know about Projection's
        # config file, so the panel reads it when it wasn't handed one.
        self._config = config or Config.load()
        # One authenticated client, so the token is fetched from 1Password once.
        # Built through `smartsheet_client`, which applies the credential
        # config.toml names — a bare `SmartsheetClient()` cannot see it, and a
        # handed-in one is used as-is by `build_backend`.
        #
        # A host embedding this panel should hand over **nothing**: which
        # credential to read comes from Projection's own config, which a host has
        # no reason to know about. Passing a bare client is how the embed ended up
        # unable to find a token that the standalone app found fine.
        self._owns_client = client is None
        self._client = client or smartsheet_client(self._config)
        # None when nothing is configured: the local store is the source of
        # record, so local-only is a supported way to run, not a failure.
        #
        # A backend that cannot be *built* is the same situation, and must not be
        # fatal: `build_backend` raises for an unknown name, a Smartsheet backend
        # with no sheet id, or an unusable D1 table name — and this constructor
        # runs inside `compose()` when another app embeds the panel, where the
        # exception took out the whole modal over one mistyped key. That is
        # exactly what `config.py` promises never happens ("a value that cannot
        # be used does not raise"), so the promise is kept here: fall back to
        # local-only and say why at mount.
        self._backend_error: str | None = None
        self._backend = self._build_backend()
        self._sync = SyncCoordinator(
            on_event=self._on_sync_event,
            poll_interval=self._config.sync.poll_interval,
            remote=self._backend,
            data_dir=self._config.data_dir,
        )
        self._projects: list[Project] = []
        self.view_kind: str = "all"  # "all" | "starred" | "status"
        self.view_status: str | None = None
        self.filter_text = ""
        self._current_option_id = ""
        self._sync_error: str | None = None  # last refresh failure, if unresolved
        # A message that owns the panel body until data arrives: loading the
        # token, or the reason it could not be loaded. Kept in the panel rather
        # than only in a toast, because a toast fades and -- when the panel is
        # embedded in another app -- can be covered by the host's own chrome.
        self._status: str | None = None
        self._vim = self._config.keys.vim
        self._last_g = 0.0  # monotonic time of the last pending `g` press
        # Serializes ListView rebuilds so concurrent populates can't
        # interleave clear()/extend() and duplicate rows.
        self._populate_lock = asyncio.Lock()
        self._hooks = {hook.id: hook for hook in self._config.hooks}
        # Keys a hook asked for but could not have, because something already
        # owns them. Announced at mount: a hook quietly shadowing `d` would take
        # delete away with no indication of why.
        self._hook_key_clashes: list[str] = []
        self._bind_hooks()

    def _build_backend(self) -> Backend | None:
        """The configured backend, or None with the reason recorded.

        Records into `_backend_error` rather than raising: see the note in
        `__init__`. Local-only always works, so a broken setting costs syncing,
        not the panel.
        """
        try:
            return build_backend(self._config, client=self._client)
        except BackendError as e:
            self._backend_error = str(e)
            return None

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                if self._show_logo:
                    yield Static(logo(), id="logo")
                yield OptionList(id="nav")
            with Vertical(id="main"):
                yield ViewHeader(id="view-header")
                yield Input(placeholder="filter this view…", id="filter")
                yield ProjectList(id="projects")
                yield Static("", id="empty")

    def on_mount(self) -> None:
        self._fit_logo()
        # A setting that fell back to its built-in default is announced, not
        # swallowed: the built-in defaults include *sheet ids*, so quietly
        # ignoring a bad value means reading someone else's sheet.
        if self._config.load_error:
            self.notify(
                f"config.toml: {self._config.load_error}",
                severity="warning",
                timeout=10,
            )
        if self._backend_error:
            # Louder than a bad setting that fell back to a default: nothing is
            # syncing, and only this message says so.
            self.notify(
                f"{self._backend_error} Running local-only — press , to fix it.",
                severity="error",
                timeout=15,
            )
        for clash in self._hook_key_clashes:
            self.notify(f"hook key: {clash}", severity="warning", timeout=10)
        self._build_nav()
        self.query_one("#nav", OptionList).focus()
        self.query_one("#projects", ListView).loading = True
        # In a worker, not awaited here: loading the token can block for as
        # long as 1Password takes to unlock, and awaiting it in on_mount holds
        # up the mount itself -- so the very message explaining the wait would
        # not paint until the wait was over.
        self.run_worker(self._load_projects(), name="load-projects")
        if self._config.first_run:
            # config.toml did not exist until this launch. That is the one moment
            # an unprompted wizard is welcome rather than in the way -- on every
            # later run, setup is a key (and a palette entry) instead.
            self.call_after_refresh(self.action_setup)

    def on_resize(self) -> None:
        self._fit_logo()

    def _fit_logo(self) -> None:
        # On short terminals the sidebar space belongs to the lists.
        if not self._show_logo:
            return  # not composed at all
        self.query_one("#logo", Static).display = self.size.height >= 20

    async def _load_projects(self) -> None:
        """Load projects - instant from local cache, then sync from remote."""
        list_view = self.query_one("#projects", ListView)
        # No backend means no credential to fetch. Loading the token anyway made
        # a local-only install prompt 1Password for nothing -- and on a machine
        # with no `op` at all it *failed*, showing an auth error in place of a
        # perfectly good local store. Local-only is the default, so that was the
        # out-of-the-box experience.
        if self._backend is not None and not await self._authenticate():
            list_view.loading = False
            return
        try:
            self._projects = await self._sync.initial_sync()
        except Exception as e:
            list_view.loading = False
            self.notify(f"Error loading projects: {e}", severity="error", timeout=10)
            return
        list_view.loading = False
        # initial_sync swallows fetch errors and falls back to the cache, so an
        # empty list here can mean "the fetch failed", not "you have no
        # projects". Check explicitly rather than inferring from the exception.
        if self._sync.last_error:
            self._sync_error = self._sync.last_error
            self.notify(self._sync_error, severity="error", timeout=15)
        self._update_views()
        self._sync.start_polling()

    async def _authenticate(self) -> bool:
        """Load the backend credential, reporting failure in the panel body.

        Done up front so a locked-1Password or missing-token problem is stated
        plainly rather than surfacing later as a generic sync failure. `op read`
        blocks while 1Password prompts, long enough that an unexplained spinner
        reads as a hang — so the wait says what it is waiting for.

        The *backend* is asked, not the Smartsheet client: which credential to
        load is the backend's business, and each one reads a different item.
        """
        if self._backend is None:
            return True
        self._set_status(
            "◌  waiting for 1Password…\n"
            "unlock the app if it is prompting for Touch ID"
        )
        try:
            await self._backend.ensure_ready()
        except Exception as e:
            self._set_status(f"⚠  {e}")
            self.notify(
                f"{self._backend.name} auth: {e}", severity="error", timeout=15
            )
            return False
        self._set_status(None)
        if secrets.token_source and secrets.token_source != secrets.OP_SOURCE:
            # Say so: a forgotten env token silently outranks 1Password and
            # keeps working after the real credential is rotated.
            self.notify(
                f"Using the token from {secrets.token_source} — not 1Password",
                severity="warning",
                timeout=10,
            )
        return True

    # -- sidebar ------------------------------------------------------------

    def _build_nav(self) -> None:
        nav = self.query_one("#nav", OptionList)
        if (
            self.view_kind in TRANSIENT_VIEWS
            and not self._in_view(self.view_kind, None)
        ):
            # The row is about to disappear; don't leave the pane pointed at it.
            self.view_kind = "all"
            self._current_option_id = ""
        selected_id = self._current_option_id or f"view:{self.view_kind}"
        nav.clear_options()
        nav.add_option(nav_header("Smart Lists"))
        for view in SMART_VIEWS:
            count = len(self._in_view(view.key, None))
            # A row that only exists when it has something in it: a permanent
            # "Conflicts 0" is clutter, one that appears is a signal.
            if count == 0 and view.key in TRANSIENT_VIEWS:
                continue
            nav.add_option(smart_option(view, count))
        nav.add_option(nav_header(""))
        nav.add_option(nav_header("Status"))
        for status in self._nav_statuses():
            count = len(self._in_view("status", status))
            nav.add_option(status_option(status, count))
        try:
            index = nav.get_option_index(selected_id)
        except Exception:
            index = 1  # first smart view
        nav.highlighted = index

    def _nav_statuses(self) -> list[str]:
        """Configured statuses plus any others observed in the sheet."""
        statuses = list(self._config.status_options)
        seen = set(statuses)
        for p in self._projects:
            if p.status not in seen:
                seen.add(p.status)
                statuses.append(p.status)
        return statuses

    @on(OptionList.OptionHighlighted, "#nav")
    def _nav_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        option_id = event.option.id
        if not option_id or option_id == self._current_option_id:
            return
        self._current_option_id = option_id
        kind, _, ref = option_id.partition(":")
        if kind == "view":
            self.view_kind = ref
            self.view_status = None
        else:
            self.view_kind = "status"
            self.view_status = ref
        self.filter_text = ""
        filter_input = self.query_one("#filter", Input)
        filter_input.value = ""
        filter_input.remove_class("-visible")
        self.repopulate()

    @on(OptionList.OptionSelected, "#nav")
    def _nav_selected(self) -> None:
        self.query_one("#projects", ListView).focus()

    # -- data / views ---------------------------------------------------------

    def _in_view(self, kind: str, status: str | None) -> list[Project]:
        """Projects belonging to a view, de-duped by project id (the identity)."""
        if kind == "starred":
            items = [p for p in self._projects if p.is_starred]
        elif kind == "conflicts":
            items = [p for p in self._projects if p.has_conflicts]
        elif kind == "status":
            items = [p for p in self._projects if p.status == status]
        else:
            items = list(self._projects)
        seen: set[str] = set()
        unique: list[Project] = []
        for p in items:
            if p.key not in seen:
                seen.add(p.key)
                unique.append(p)
        return unique

    def _visible_projects(self) -> list[Project]:
        items = self._in_view(self.view_kind, self.view_status)
        if self.filter_text:
            items = [p for p in items if p.matches(self.filter_text)]
        return sort_projects(items)

    def _update_views(self) -> None:
        """Refresh sidebar counts and the project list from current data."""
        self._build_nav()
        self.repopulate()

    # Deliberately not exclusive. Cancelling this worker part-way through
    # rebuilding the ListView ends the app's message loop -- silently, with no
    # exception and no exit() call. remtui had the identical construct and hit
    # exactly that once the worker belonged to a widget rather than the App.
    # _populate_lock already serializes rebuilds, so exclusivity bought only the
    # cancellation.
    @work(group="populate")
    async def repopulate(self) -> None:
        await self._populate()

    async def _populate(self) -> None:
        async with self._populate_lock:
            list_view = self.query_one("#projects", ListView)
            shown = self._visible_projects()
            previous_key = self._selected_key()
            previous_index = list_view.index or 0
            await list_view.clear()
            await list_view.extend(ProjectItem(p) for p in shown)
            if shown:
                # Reselect the same project; if it's gone (moved to another
                # status or deleted), stay near its old position instead of
                # jumping to the top.
                index = next(
                    (i for i, p in enumerate(shown) if p.key == previous_key),
                    min(previous_index, len(shown) - 1),
                )
                list_view.index = index
            self._update_header(len(shown))
            self._update_empty(bool(shown))
            # Selection / sync-flag availability may have changed; let the
            # footer re-evaluate its grayed-out states.
            self.refresh_bindings()

    def _sync_note(self) -> Text | None:
        """Header note about remote-sync health: failure, staleness, or none."""
        last = self._sync.last_sync()
        if self._sync_error:
            cached = f" — showing cached data from {humanize_age(last)}" if last else ""
            return Text(
                f"⚠ refresh failing{cached} (r to retry)",
                style=f"bold {COLOR_OVERDUE}",
            )
        age = sync_age_seconds(last)
        if age is not None and age > _STALE_AFTER_SECONDS:
            return Text(f"synced {humanize_age(last)}", style="dim")
        return None

    def _update_header(self, shown: int) -> None:
        header = self.query_one("#view-header", ViewHeader)
        sync_note = self._sync_note()
        if self.view_kind == "status" and self.view_status is not None:
            header.show_view(
                label=self.view_status,
                icon=status_icon(self.view_status),
                color=status_color(self.view_status),
                shown=shown,
                filter_text=self.filter_text,
                sync_note=sync_note,
            )
        else:
            view = _SMART_BY_KEY.get(self.view_kind, SMART_VIEWS[0])
            active = completed = None
            if view.key == "all":
                pool = self._in_view("all", None)
                active = sum(1 for p in pool if p.status not in DONE_STATUSES)
                completed = sum(1 for p in pool if p.status == DONE_STATUS)
            header.show_view(
                label=view.label,
                icon=view.icon,
                color=view.color,
                shown=shown,
                active=active,
                completed=completed,
                filter_text=self.filter_text,
                sync_note=sync_note,
            )

    def _set_status(self, message: str | None) -> None:
        """Show (or clear) the message that owns the panel body."""
        self._status = message
        try:
            self._update_empty(has_items=False if message else bool(self._projects))
        except NoMatches:
            pass  # not mounted yet; on_mount renders it

    def _update_empty(self, has_items: bool) -> None:
        empty = self.query_one("#empty", Static)
        if self._status is not None:
            # Outranks the per-view empty text: "no done projects" is wrong
            # -- and misleading -- when nothing could be loaded at all.
            empty.update(self._status)
            empty.add_class("-visible")
            return
        if has_items:
            empty.remove_class("-visible")
            return
        if self.filter_text:
            message = f'○  nothing matches "{self.filter_text}"'
        elif self.view_kind == "status":
            message = f"○  no {self.view_status.lower()} projects"
        else:
            view = _SMART_BY_KEY.get(self.view_kind)
            message = f"○  {view.empty}" if view else "○  nothing here"
            if not self._sync.has_backend:
                # An empty store with no backend is the out-of-the-box state, and
                # the only place a first-time user is looking. Say how to connect
                # one here rather than leaving the option undiscoverable.
                message += "\n\n[dim]n to add one, or , to connect a backend[/]"
        empty.update(message)
        empty.add_class("-visible")

    # -- sync events ----------------------------------------------------------

    def _on_sync_event(self, event: SyncEvent) -> None:
        """Handle sync events from the coordinator."""
        self.call_later(self._handle_sync_event, event)

    def _handle_sync_event(self, event: SyncEvent) -> None:
        """Process sync events on the main thread."""
        if event.event_type == "sync_started":
            pass  # Silent for background syncs
        elif event.event_type == "sync_complete":
            self._sync_error = None
            if event.data is not None:
                self._projects = event.data
                self._update_views()
            else:
                self.repopulate()  # clear the header's failure note
        elif event.event_type == "sync_error":
            self._sync_error = event.message
            self.notify(event.message, severity="warning", timeout=3)
            self.repopulate()  # surface the failure note in the header
        elif event.event_type == "conflict":
            # Loud on purpose: a conflict is the one sync outcome that needs a
            # decision, and nothing is pushed for those fields until it is made.
            self.notify(event.message, severity="warning", timeout=10)
        elif event.event_type == "data_updated":
            self._projects = self._sync.load()
            self._update_views()

    # -- selection helpers ------------------------------------------------------

    def _selected_project(self) -> Project | None:
        try:
            item = self.query_one("#projects", ListView).highlighted_child
        except Exception:
            # check_action can run before the widget tree is mounted.
            return None
        if isinstance(item, ProjectItem):
            return item.project
        return None

    def _selected_key(self) -> str | None:
        project = self._selected_project()
        return project.key if project else None

    # -- mutations --------------------------------------------------------------

    def action_new_project(self) -> None:
        """Create a new project."""
        self.app.push_screen(
            EditModal(
                project=None,
                status_options=list(self._config.status_options),
                contact_options=self._sync.assignee_options(),
            ),
            self._on_edit_complete,
        )

    def action_edit(self) -> None:
        """Edit the selected project."""
        project = self._selected_project()
        if project:
            self.app.push_screen(
                EditModal(
                    project,
                    status_options=list(self._config.status_options),
                    contact_options=self._sync.assignee_options(),
                ),
                self._on_edit_complete,
            )
        else:
            self.notify("No project selected", severity="warning")

    @on(ListView.Selected, "#projects")
    def _project_selected(self) -> None:
        self.action_edit()

    def action_delete(self) -> None:
        """Delete the selected project."""
        project = self._selected_project()
        if not project:
            self.notify("No project selected", severity="warning")
            return

        async def on_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                if await self._sync.delete_item(project.key):
                    self.notify(f"Deleted: {project.title}")
                else:
                    self.notify(
                        f"{project.title!r} was already gone", severity="warning"
                    )
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

        self.app.push_screen(ConfirmDeleteModal(project), on_confirm)

    async def action_toggle_done(self) -> None:
        """Toggle the selected project between Done and In progress."""
        project = self._selected_project()
        if not project:
            return
        if project.status == DONE_STATUS:
            new_status, note = "In progress", f"↺ Reopened “{project.title}”"
        else:
            new_status, note = DONE_STATUS, f"✓ Done “{project.title}”"
        try:
            await self._sync.update_item(project.key, status=new_status)
            self.notify(note, timeout=3)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    async def action_toggle_star(self) -> None:
        """Star or unstar the selected project."""
        project = self._selected_project()
        if not project:
            return
        starred = not project.is_starred
        try:
            await self._sync.toggle_starred(project.key, starred)
            state = "starred" if starred else "unstarred"
            self.notify(f"⟳ {project.title}: {state}", timeout=3)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    async def action_resolve_conflicts(self) -> None:
        """Choose, per field, whose value wins on the selected project."""
        project = self._selected_project()
        if project is None:
            self.notify("No project selected", severity="warning")
            return
        if not project.has_conflicts:
            self.notify(f"No conflicts on {project.title!r}")
            return

        async def on_choice(chosen: dict[str, bool] | None) -> None:
            if not chosen:
                return  # cancelled: every conflict stays exactly as it was
            resolved = 0
            for name, take_theirs in chosen.items():
                if await self._sync.resolve_conflict(
                    project.key, name, take_theirs=take_theirs
                ):
                    resolved += 1
            if resolved:
                self.notify(f"Resolved {resolved} field(s) on {project.title}")

        self.app.push_screen(ConflictModal(project), on_choice)

    async def _on_edit_complete(self, result: EditResult | None) -> None:
        """Handle the result of editing or creating a project."""
        if result is None:
            return  # User cancelled

        # Ctrl+D in the modal means "save as done".
        status = DONE_STATUS if result.completed else result.status

        try:
            if result.is_new:
                await self._sync.add_item(
                    title=result.title,
                    status=status,
                    assigned=result.assigned,
                    due_date=result.due_date,
                    note=result.note,
                    starred=result.starred,
                )
                self.notify(f"Created: {result.title}")
            else:
                ok = await self._sync.update_item(
                    result.key,
                    title=result.title,
                    status=status,
                    assigned=result.assigned,
                    due_date=result.due_date,
                    note=result.note,
                    starred=result.starred,
                )
                if ok:
                    self.notify(f"Updated: {result.title}")
                else:
                    self.notify(
                        f"Could not update {result.title!r} — it is no longer "
                        "in the local cache",
                        severity="error",
                    )
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    async def action_refresh(self) -> None:
        """Force refresh from the backend."""
        if not self._sync.has_backend:
            self.notify(
                "No backend configured — the local store is the source of "
                "record. Set `backend` in config.toml to sync.",
            )
            return
        loading = LoadingModal(f"Refreshing from {self._sync.backend_name}…")
        self.app.push_screen(loading)
        try:
            self._projects = await self._sync.refresh()
            loading.dismiss()
            self._update_views()
            # refresh() returns the cache on failure, so a row count alone
            # would cheerfully report success over a failed fetch.
            if self._sync.last_error:
                self._sync_error = self._sync.last_error
                self.notify(self._sync_error, severity="error", timeout=10)
            else:
                self.notify(f"Loaded {len(self._projects)} items")
        except Exception as e:
            loading.dismiss()
            self.notify(f"Error refreshing: {e}", severity="error")

    # ==================== Setup ====================

    def action_setup(self) -> None:
        """Choose a backend, and create or connect its target."""
        # A worker, because the flow is a sequence of dialogs: `push_screen_wait`
        # can only be awaited off the message loop.
        self.run_worker(self._setup_flow(), name="setup", exit_on_error=False)

    async def _setup_flow(self) -> None:
        choice = await self.app.push_screen_wait(
            SetupModal(self._config, probe=self._probe_choice)
        )
        if choice is None:
            return
        if self._sync.busy:
            # Swapping the coordinator out from under an in-flight write would
            # apply its bookkeeping to a store this coordinator no longer owns.
            self.notify(
                "A write is still in flight — try setup again in a moment.",
                severity="warning",
            )
            return
        try:
            resolved = await self._resolve_setup(choice)
        except Exception as e:
            self.notify(f"Setup failed: {e}", severity="error", timeout=15)
            return
        if resolved is None:
            return  # answered in the dialog, nothing to save
        config, backend = resolved
        config.save()
        self._config = config
        # Reusing the backend setup already built and authenticated. Building a
        # fresh one would open a second transport and read the credential again —
        # a second `op read`, and a second chance to sit at a Touch ID prompt.
        await self._restart_sync(backend)

    async def _probe_choice(self, choice: SetupChoice) -> ProbeResult:
        """Check an entered target, for the wizard's Test button."""
        backend = self._backend_for(choice)
        if backend is None:
            return ProbeResult(ready=True, exists=True, detail="local only")
        await backend.ensure_ready()
        return await backend.probe()

    def _backend_for(self, choice: SetupChoice, config: Config | None = None):
        """A backend built from a choice, allowed to have no target yet."""
        return build_backend(
            config or self._config_for(choice),
            client=self._client,
            allow_unprovisioned=True,
        )

    def _config_for(self, choice: SetupChoice) -> Config:
        """The configuration this choice describes, unsaved.

        Only the chosen backend's settings are rewritten — see
        `Config.with_backend_values`. Turning a backend off keeps its settings, so
        turning it back on does not mean re-entering ids.
        """
        config = dataclasses.replace(
            self._config,
            backend=choice.backend,
            # Whatever happens next, the file exists from here on.
            first_run=False,
        )
        if choice.is_local_only:
            return config
        return config.with_backend_values(
            choice.backend, choice.values, columns=choice.columns
        )

    async def _resolve_setup(
        self, choice: SetupChoice
    ) -> tuple[Config, Backend | None] | None:
        """Do whatever the choice needs, returning the config to save.

        Also returns the backend it built and authenticated along the way, so the
        caller can keep it rather than opening a second transport and reading the
        credential again.

        None means the answer is already on screen and nothing should be
        written: a half-configured backend saved to config.toml is worse than
        no change, since the next launch would fail on it.
        """
        config = self._config_for(choice)
        if choice.is_local_only:
            return config, None

        backend = self._backend_for(choice, config)
        assert backend is not None  # not local-only
        await backend.ensure_ready()

        if choice.create:
            result = await backend.provision()
            self.notify(result.detail or "Created it")
            # The id the backend just got is the whole point of provisioning; a
            # target created but never written to config.toml is unreachable.
            return self._with_target(config, choice.backend, result), backend

        probe = await backend.probe()

        if not probe.ready and probe.repairable and backend.capabilities.can_provision:
            # Reachable, but missing structure the backend owns and can create —
            # a D1 database with no table in it. Nothing to ask the user here: the
            # alternative is telling them to go and run the DDL themselves.
            result = await backend.provision()
            self.notify(result.detail or "Created the missing structure")
            config = self._with_target(config, choice.backend, result)
            probe = await backend.probe()

        if probe.ready:
            return config, backend
        if not probe.exists:
            # Nothing there to map. Wrong id, no access, or the API is down —
            # all of which the detail already says.
            self.notify(
                probe.detail or "That target is not reachable",
                severity="error",
                timeout=15,
            )
            return None

        # It exists, but its columns are not named the way Projection names its
        # fields — the one case a mapping answers.
        titles = await backend.target_columns()
        if not titles:
            self.notify(
                probe.detail or "That target is missing columns",
                severity="error",
                timeout=15,
            )
            return None
        mapping = await self.app.push_screen_wait(
            ColumnMapModal(
                titles,
                choice.columns,
                target_name=str(
                    choice.values.get("sheet_name")
                    or choice.values.get("database_name")
                    or ""
                ),
            )
        )
        if mapping is None:
            return None  # cancelled the mapping, so nothing is configured

        mapped = config.with_backend_values(
            choice.backend, choice.values, columns=dict(mapping)
        )
        remapped = build_backend(mapped, client=self._client)
        assert remapped is not None
        # Probe again rather than assume: a mapping can name the wrong column
        # just as easily as a config file can.
        second = await remapped.probe()
        if not second.ready:
            self.notify(
                second.detail or "That mapping does not fit the target",
                severity="error",
                timeout=15,
            )
            return None
        return mapped, remapped

    def _with_target(self, config: Config, backend: str, result) -> Config:
        """`config` with the id and name a provision just produced."""
        return config.with_provisioned_target(backend, result.target_id, result.name)

    async def _restart_sync(self, backend: Backend | None = None) -> None:
        """Rebuild the coordinator for `self._config`'s backend and reconcile.

        A new coordinator rather than a mutated one: `SyncCoordinator` holds a
        backend name, in-flight guards, and a poll task that all belong to one
        backend, and rewriting those in place is how half of a switch happens.

        `backend` adopts one that has already been built and authenticated —
        setup's, which is pointed at the config just saved.
        """
        self._sync.stop_polling()
        self._backend_error = None
        # Same fallback as at construction: a config that cannot build a backend
        # leaves the panel usable and local-only rather than throwing.
        self._backend = backend or self._build_backend()
        if self._backend_error:
            self.notify(self._backend_error, severity="error", timeout=15)
        self._sync = SyncCoordinator(
            on_event=self._on_sync_event,
            poll_interval=self._config.sync.poll_interval,
            remote=self._backend,
            data_dir=self._config.data_dir,
        )
        self._sync_error = None

        if self._backend is None:
            self._projects = await self._sync.initial_sync()
            self.notify(
                "Local only — your projects stay in the local store and nothing "
                "is synced."
            )
        else:
            loading = LoadingModal(f"Reconciling with {self._sync.backend_name}…")
            self.app.push_screen(loading)
            try:
                report = await self._sync.adopt()
                self.notify(f"Connected {self._sync.backend_name}: {report.summary}")
            except Exception as e:
                # The local store is untouched either way; what changed is that
                # the configured backend cannot be reached. Say so, rather than
                # letting the panel imply everything is in step.
                self._sync_error = f"Sync failed: {e}"
                self.notify(self._sync_error, severity="error", timeout=15)
            finally:
                loading.dismiss()
            self._projects = self._sync.load()

        self._update_views()
        self._sync.start_polling()

    # -- filtering ---------------------------------------------------------------

    def action_show_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        filter_input.add_class("-visible")
        filter_input.focus()

    def action_dismiss_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        if self.filter_text or filter_input.has_class("-visible"):
            filter_input.value = ""
            filter_input.remove_class("-visible")
            self.filter_text = ""
            self.repopulate()
            self.query_one("#projects", ListView).focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self.repopulate()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        self.query_one("#projects", ListView).focus()

    # -- navigation ---------------------------------------------------------------

    def _modal_is_open(self) -> bool:
        """Whether a screen is stacked above the one holding this panel.

        Answerable before mounting, where there is no app to ask.
        """
        if not self.is_mounted:
            return False
        return self.app.screen is not self.screen

    def action_vim_down(self) -> None:
        self._vim_move(1)

    def action_vim_up(self) -> None:
        self._vim_move(-1)

    def _vim_move(self, delta: int) -> None:
        focused = self.app.focused
        if isinstance(focused, (ListView, OptionList)):
            if delta > 0:
                focused.action_cursor_down()
            else:
                focused.action_cursor_up()

    def action_focus_nav(self) -> None:
        self.query_one("#nav", OptionList).focus()

    def action_focus_projects(self) -> None:
        self.query_one("#projects", ListView).focus()

    def action_toggle_pane(self) -> None:
        if isinstance(self.app.focused, OptionList):
            self.action_focus_projects()
        else:
            self.action_focus_nav()

    def action_go_top(self) -> None:
        # In the vim profile `g` is a prefix: only the gg chord jumps.
        if self._vim:
            now = time.monotonic()
            if now - self._last_g > self._GG_CHORD_SECONDS:
                self._last_g = now
                return
            self._last_g = 0.0
        self._jump(top=True)

    def action_go_bottom(self) -> None:
        self._jump(top=False)

    def _jump(self, *, top: bool) -> None:
        focused = self.app.focused
        if isinstance(focused, ListView) and len(focused) > 0:
            focused.index = 0 if top else len(focused) - 1
        elif isinstance(focused, OptionList):
            if top:
                focused.action_first()
            else:
                focused.action_last()

    # -- vim profile extras ---------------------------------------------------------

    def action_half_page_down(self) -> None:
        self._cursor_page(1, 0.5)

    def action_half_page_up(self) -> None:
        self._cursor_page(-1, 0.5)

    def action_cursor_page_down(self) -> None:
        self._cursor_page(1, 1.0)

    def action_cursor_page_up(self) -> None:
        self._cursor_page(-1, 1.0)

    def _cursor_page(self, direction: int, fraction: float) -> None:
        focused = self.app.focused
        if isinstance(focused, ProjectList):
            focused.cursor_page(direction, fraction)
        elif isinstance(focused, OptionList):
            # The sidebar is short; half and full pages both map to a page
            # (looping cursor_down would wrap past the ends).
            if direction > 0:
                focused.action_page_down()
            else:
                focused.action_page_up()

    def action_vim_palette(self) -> None:
        self.action_command_palette()

    def action_vim_new(self) -> None:
        self.action_new_project()

    # -- help / palette / dynamic bindings --------------------------------------------

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def check_action(self, action: str, parameters) -> bool | None:
        if action in self._VIM_ACTIONS and not self._vim:
            return False
        if action in self._MAIN_SCREEN_ACTIONS and self._modal_is_open():
            return False
        if action in self._SELECTION_ACTIONS and self._selected_project() is None:
            return None  # disabled, shown grayed in the footer
        if action == "run_hook":
            hook = self._hooks.get(parameters[0] if parameters else "")
            if hook is None:
                return False
            # Grayed rather than hidden when there is nothing to send: the key
            # exists, it just has no input right now.
            return True if self._hook_input(hook) else None
        return True

    # ==================== Hooks ====================

    def _bind_hooks(self) -> None:
        """Give each configured hook its key.

        Bound per instance rather than in `BINDINGS`, which is a class attribute
        fixed at import — hooks come from config, which is read later.
        """
        for hook in self._config.hooks:
            if not hook.key:
                continue
            try:
                taken = self._bindings.get_bindings_for_key(hook.key)
            except NoBinding:
                taken = []
            if taken:
                self._hook_key_clashes.append(
                    f"{hook.key!r} is already {taken[0].action!r}, so the "
                    f"{hook.id!r} hook has no key"
                )
                continue
            # No tooltip: `BindingsMap.bind` takes a narrower set of
            # arguments than the `Binding` constructor does.
            self._bindings.bind(hook.key, hook.action, hook.display)

    def _hook_input(self, hook: hooks.Hook) -> list[Project]:
        """The projects a hook would receive right now."""
        return hooks.select_projects(
            hook, projects=self._projects, selected=self._selected_project()
        )

    async def action_run_hook(self, hook_id: str) -> None:
        """Run a user script over the projects it asked for."""
        hook = self._hooks.get(hook_id)
        if hook is None:
            self.notify(f"No hook called {hook_id!r}", severity="error")
            return

        chosen = self._hook_input(hook)
        if not chosen:
            self.notify(
                f"{hook.display}: nothing to send (input = {hook.input!r})",
                severity="warning",
            )
            return

        draft = await self._run_hook_phase(
            hook, hooks.PHASE_DRAFT, chosen, f"Running {hook.display}…"
        )
        if draft is None:
            return

        if not hook.wants_review:
            self.notify(_hook_message(draft, f"{hook.display} finished"))
            return

        text = draft.strip()
        if not text:
            self.notify(
                f"{hook.display} produced nothing to review", severity="warning"
            )
            return

        async def on_reviewed(approved: str | None) -> None:
            # Cancelling means the commit phase never runs at all, which is the
            # whole reason the script is invoked twice.
            if not approved:
                self.notify(f"{hook.display}: cancelled, nothing committed")
                return
            result = await self._run_hook_phase(
                hook,
                hooks.PHASE_COMMIT,
                chosen,
                f"{hook.display}: committing…",
                text=approved,
            )
            if result is not None:
                self.notify(_hook_message(result, f"{hook.display} committed"))

        self.app.push_screen(
            ReviewModal(
                text,
                title=hook.review_title or f"Review — {hook.display}",
                approve_label="Commit",
            ),
            on_reviewed,
        )

    async def _run_hook_phase(
        self,
        hook: hooks.Hook,
        phase: str,
        chosen: list[Project],
        waiting: str,
        *,
        text: str | None = None,
    ) -> str | None:
        """Run one phase behind a loading modal. None means it failed."""
        loading = LoadingModal(waiting)
        self.app.push_screen(loading)
        try:
            return await hooks.run_hook(
                hook, phase=phase, projects=chosen, text=text
            )
        except Exception as e:
            self.notify(f"{hook.display} failed: {e}", severity="error", timeout=10)
            return None
        finally:
            loading.dismiss()

    async def on_unmount(self) -> None:
        """Stop polling when the panel goes away, and close what it owns.

        A client the panel was *handed* belongs to whoever made it — the app, for
        standalone use — and may outlive this panel, so it is left alone. One the
        panel built itself has no other owner, and an embedded panel is opened and
        closed repeatedly, so leaving those open leaks a session per visit.
        """
        self._sync.stop_polling()
        if self._owns_client:
            await self._client.aclose()


def _hook_message(output: str, fallback: str) -> str:
    """A hook's stdout as a notification, or a fallback when it said nothing."""
    text = (output or "").strip()
    if not text:
        return fallback
    # A script may print a lot; a toast is not a log viewer.
    return text if len(text) <= 400 else text[:397] + "…"
