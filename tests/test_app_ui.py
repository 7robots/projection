"""Lightweight Textual Pilot tests for view wiring (no network / no real cache)."""

import asyncio

import pytest

from textual.widgets import ListView, OptionList

from projection import panel as panel_module
from projection.app import ProjectsApp
from projection.config import Config, KeysConfig, SmartsheetConfig
from projection.models import Project, ProjectFields
from projection.sync import SyncEvent
from projection.views import LoadingModal, ReviewModal
from projection.widgets import ProjectItem


class FakeClient:
    """Smartsheet client that never authenticates or hits the network."""

    async def ensure_ready(self):
        return None

    async def aclose(self):
        return None


def synced_config(**kwargs):
    """A config that names a backend, so the credential path is exercised.

    Without a backend there is nothing to authenticate against, and the panel
    deliberately never touches 1Password — which is the whole point of
    local-only, and means these tests have to opt in.
    """
    return Config(
        backend="smartsheet",
        smartsheet=SmartsheetConfig(projects_sheet_id=1),
        **kwargs,
    )


def make_app(**kwargs):
    # An explicit config unless a test supplies one: `Config.load()` would
    # *create* config.toml under the isolated config dir, making every test a
    # first run — which now opens the setup wizard over the panel.
    kwargs.setdefault("config", Config())
    return ProjectsApp(client=FakeClient(), **kwargs)


def make_panel(**kwargs):
    """An unmounted panel, for tests that do not need a running app."""
    kwargs.setdefault("config", Config())
    return panel_module.ProjectsPanel(client=FakeClient(), **kwargs)

LONG_UPDATE = (
    "Vendor selected and pilot deployed to 40 users.\n"
    "Rollout to the remaining schools is scheduled for August; "
    "waiting on the identity team's SCIM connector before broad enablement."
)


def _project(title, remote_id=None, **fields):
    """A project as the store would hold it: local id, backend key mapped."""
    project = Project(fields=ProjectFields(title=title, **fields))
    if remote_id is not None:
        project.link_remote("smartsheet", remote_id)
    return project


class FakeSync:
    """Stand-in SyncCoordinator that serves canned data."""

    def __init__(self, *args, on_event=None, **kwargs):
        self._on_event = on_event
        self._projects = [
            _project("ZTNA", 1, status="In progress", note=LONG_UPDATE),
            _project("AI Assistant", 2, status="In progress", starred=True),
            _project("Old Migration", 3, status="Done"),
        ]

    async def initial_sync(self):
        return list(self._projects)

    async def refresh(self):
        await asyncio.sleep(0)  # yield so the loading modal mounts
        return list(self._projects)

    def load(self):
        return list(self._projects)

    # The panel asks whether there is anything to sync with at all.
    has_backend = True
    backend_name = "smartsheet"

    def conflicted(self):
        return [p for p in self._projects if p.has_conflicts]

    async def resolve_conflict(self, key, field_name, *, take_theirs):
        return False

    def start_polling(self):
        pass

    def stop_polling(self):
        pass

    def last_sync(self):
        return None

    @property
    def last_error(self):
        return None

    def assignee_options(self):
        return ["Ada Lovelace", "Grace Hopper"]

    def _emit_data_updated(self):
        if self._on_event:
            self._on_event(SyncEvent(event_type="data_updated"))

    async def update_item(self, key, **kwargs):
        return True

    async def delete_item(self, key):
        self._projects = [p for p in self._projects if p.key != key]
        self._emit_data_updated()
        return True

    async def toggle_starred(self, key, starred):
        return await self.update_item(key, starred=starred)


def test_project_item_body_contains_full_update():
    project = _project("ZTNA", None, status="In progress", note=LONG_UPDATE)
    body = ProjectItem(project)._body().plain
    # The status update must render in full — no truncation.
    for line in LONG_UPDATE.splitlines():
        assert line in body


async def test_list_and_sidebar_mount(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        list_view = app.query_one("#projects", ListView)
        assert len(list_view) == 3  # "All Projects" view shows everything
        assert app.panel._selected_project() is not None
        nav = app.query_one("#nav", OptionList)
        # Smart lists + statuses are all present.
        assert nav.get_option_index("view:all") is not None
        assert nav.get_option_index("status:Done") is not None


async def test_status_smart_list_filters_projects(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        nav = app.query_one("#nav", OptionList)
        nav.highlighted = nav.get_option_index("status:Done")
        await pilot.pause()
        list_view = app.query_one("#projects", ListView)
        assert len(list_view) == 1
        assert app.panel._selected_project().title == "Old Migration"

        nav.highlighted = nav.get_option_index("status:In progress")
        await pilot.pause()
        assert len(app.query_one("#projects", ListView)) == 2


async def test_starred_smart_list_shows_starred_only(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        nav = app.query_one("#nav", OptionList)
        nav.highlighted = nav.get_option_index("view:starred")
        await pilot.pause()
        list_view = app.query_one("#projects", ListView)
        assert len(list_view) == 1
        assert app.panel._selected_project().title == "AI Assistant"


async def test_filter_narrows_view(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#projects", ListView).focus()
        await pilot.press("slash")
        await pilot.press("z", "t", "n", "a")
        await pilot.pause()
        assert len(app.query_one("#projects", ListView)) == 1
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.query_one("#projects", ListView)) == 3


async def test_delete_confirm_enter_is_safe(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#projects", ListView).focus()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("enter")  # activates the focused Cancel button
        await pilot.pause()
        # Enter must neither delete nor leak through and open the editor.
        assert app.screen is app.screen_stack[0]
        assert len(app.query_one("#projects", ListView)) == 3


async def test_delete_confirm_y_deletes(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#projects", ListView).focus()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert len(app.query_one("#projects", ListView)) == 2


async def test_app_keys_gated_while_modal_open(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#projects", ListView).focus()
        await pilot.press("d")
        await pilot.pause()
        # "a" (new project) must not stack another modal over the confirm.
        await pilot.press("a")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("n")  # cancel


async def test_vim_profile_gg_and_paging(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app(config=Config(keys=KeysConfig(profile="vim")))
    async with app.run_test() as pilot:
        await pilot.pause()
        list_view = app.query_one("#projects", ListView)
        list_view.focus()
        list_view.index = 2
        # Single g is a prefix in vim mode: no jump.
        await pilot.press("g")
        assert list_view.index == 2
        # gg jumps to the top.
        await pilot.press("g")
        assert list_view.index == 0
        # ctrl+d moves the selection (half page down).
        await pilot.press("ctrl+d")
        assert list_view.index > 0


async def test_default_profile_has_no_vim_extras(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app(config=Config(keys=KeysConfig(profile="default")))
    async with app.run_test() as pilot:
        await pilot.pause()
        list_view = app.query_one("#projects", ListView)
        list_view.focus()
        list_view.index = 2
        await pilot.press("g")  # jumps immediately, no chord
        assert list_view.index == 0
        await pilot.press("ctrl+d")  # vim extra: disabled in default profile
        assert list_view.index == 0
        await pilot.press("o")  # vim extra: no new-project modal
        await pilot.pause()
        assert app.screen is app.screen_stack[0]


async def test_keymap_override_rebinds_action(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app(config=Config(keys=KeysConfig(overrides={"project.new": "w"})))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#projects", ListView).focus()
        await pilot.press("w")
        await pilot.pause()
        assert len(app.screen_stack) == 2  # edit modal opened via override


async def test_palette_lists_app_commands(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        titles = {c.title for c in app.get_system_commands(app.screen)}
        assert {"New project", "Refresh projects"} <= titles


def test_check_action_grays_selection_actions_without_data():
    # Unmounted panel: nothing selected. Built directly, since there is no
    # screen to reach the panel through before mounting.
    panel = make_panel()
    assert panel.check_action("edit", ()) is None
    assert panel.check_action("quit", ()) is True


async def test_tab_toggles_between_panes(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.focused, OptionList)
        await pilot.press("tab")
        assert isinstance(app.focused, ListView)
        await pilot.press("tab")
        assert isinstance(app.focused, OptionList)


async def test_sync_error_shows_and_clears_header_note(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.panel._sync_note() is None
        app.panel._handle_sync_event(SyncEvent(event_type="sync_error", message="boom"))
        await pilot.pause()
        note = app.panel._sync_note()
        assert note is not None and "refresh failing" in note.plain
        # Any successful sync clears the failure note.
        app.panel._handle_sync_event(SyncEvent(event_type="sync_complete", data=None))
        await pilot.pause()
        assert app.panel._sync_note() is None


def test_stale_cache_note(monkeypatch):
    from datetime import datetime, timedelta
    from projection.widgets import humanize_age, sync_age_seconds

    recent = datetime.now().isoformat()
    old = (datetime.now() - timedelta(days=3)).isoformat()
    assert sync_age_seconds(None) is None
    assert sync_age_seconds(recent) < 60
    assert humanize_age(old) == "3d ago"
    assert humanize_age("not-a-date") == "unknown"

    panel = make_panel()
    panel._sync.last_sync = lambda: old  # type: ignore[method-assign]
    note = panel._sync_note()
    assert note is not None and "3d ago" in note.plain
    panel._sync.last_sync = lambda: recent  # type: ignore[method-assign]
    assert panel._sync_note() is None


# ==================== Hooks ====================


def _hook_config(tmp_path, body, **hook):
    """A config with one hook whose script is `body`."""
    import stat

    from projection.config import Config
    from projection.hooks import Hook

    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    hook.setdefault("id", "test-hook")
    hook.setdefault("label", "Test hook")
    hook.setdefault("key", "x")
    return Config(hooks=(Hook(command=(str(script),), **hook),))


async def test_a_hook_key_is_bound_and_runs_the_script(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    marker = tmp_path / "ran.txt"
    config = _hook_config(tmp_path, f'cat > "{marker}"; echo done')
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        for _ in range(6):
            await pilot.pause()
    # The payload reached the script on stdin.
    assert "ZTNA" in marker.read_text()


async def test_a_fire_hook_needs_no_review(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    config = _hook_config(tmp_path, "echo all good", mode="fire")
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        for _ in range(6):
            await pilot.pause()
        assert app.screen is app.screen_stack[0]


async def test_a_review_hook_opens_the_review_modal(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    config = _hook_config(tmp_path, "echo the draft", mode="review")
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        for _ in range(8):
            await pilot.pause()
        assert isinstance(app.screen, ReviewModal)


async def test_approving_runs_the_commit_phase_with_the_edited_text(
    monkeypatch, tmp_path
):
    """What the review returns is exactly what the commit phase receives."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    log = tmp_path / "phases.txt"
    config = _hook_config(
        tmp_path,
        f'echo "$1" >> "{log}"; cat >> "{log}"; echo the draft',
        mode="review",
    )
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        for _ in range(8):
            await pilot.pause()
        assert isinstance(app.screen, ReviewModal)
        await pilot.press("ctrl+s")
        for _ in range(8):
            await pilot.pause()

    written = log.read_text()
    assert "--phase=draft" in written
    assert "--phase=commit" in written
    assert '"text": "the draft"' in written


async def test_cancelling_the_review_never_commits(monkeypatch, tmp_path):
    """The reason the script is invoked twice rather than once."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    log = tmp_path / "phases.txt"
    config = _hook_config(
        tmp_path, f'echo "$1" >> "{log}"; echo the draft', mode="review"
    )
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        for _ in range(8):
            await pilot.pause()
        await pilot.press("escape")
        for _ in range(6):
            await pilot.pause()

    assert log.read_text().count("--phase=commit") == 0


async def test_a_failing_hook_reports_and_opens_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    config = _hook_config(tmp_path, "echo it broke 1>&2; exit 1", mode="review")
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        for _ in range(8):
            await pilot.pause()
        # No review modal, and the loading modal is gone too.
        assert app.screen is app.screen_stack[0]


async def test_a_hook_key_that_is_already_taken_is_refused(monkeypatch, tmp_path):
    """A hook silently shadowing `d` would take delete away with no clue why."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    config = _hook_config(tmp_path, "echo hi", key="d")
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.panel
        assert panel._hook_key_clashes
        assert "delete" in panel._hook_key_clashes[0]
        # And `d` still deletes.
        app.query_one("#projects", ListView).focus()
        await pilot.press("d")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmDeleteModal"


async def test_a_hook_with_no_input_is_grayed_not_fired(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    marker = tmp_path / "ran.txt"
    config = _hook_config(
        tmp_path, f'echo ran > "{marker}"', input="conflicts"
    )
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.panel
        assert panel.check_action("run_hook", ("test-hook",)) is None
        await pilot.press("x")
        for _ in range(5):
            await pilot.pause()
    assert not marker.exists()


async def test_the_palette_lists_each_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    config = _hook_config(tmp_path, "echo hi", label="Executive summary")
    app = make_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        titles = {c.title for c in app.get_system_commands(app.screen)}
        assert "Run: Executive summary" in titles


async def test_r_refresh_clears_loading_modal(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        # The loading modal is dismissed once the refresh completes.
        assert not isinstance(app.screen, LoadingModal)


async def test_q_quits_the_standalone_app(monkeypatch):
    """A widget binding's action resolves against the widget, so the panel's
    `q` has to name the app explicitly or quit silently does nothing."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#projects", ListView).focus()

        await pilot.press("q")
        await pilot.pause()

        assert not app.is_running


# ── status message: what the panel says while it has no data ──────────────


class SlowAuthClient(FakeClient):
    """A client whose auth blocks, like `op read` against a locked 1Password."""

    def __init__(self):
        self.released = asyncio.Event()

    async def ensure_ready(self):
        await self.released.wait()


class FailingAuthClient(FakeClient):
    def __init__(self, message="1Password is locked. Unlock the app and relaunch."):
        self.message = message

    async def ensure_ready(self):
        raise RuntimeError(self.message)


async def test_panel_says_it_is_waiting_on_1password(monkeypatch):
    """A locked vault blocks `op read` for up to 90s.

    An unexplained spinner for that long reads as a hang, which is exactly how
    it was reported.
    """
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    client = SlowAuthClient()
    app = ProjectsApp(client=client, config=synced_config())

    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(5):
            await pilot.pause()

        panel = app.screen.query_one(panel_module.ProjectsPanel)
        empty = panel.query_one("#empty")

        assert "1Password" in str(empty.render())
        assert empty.has_class("-visible")

        client.released.set()  # let it finish so teardown is clean
        for _ in range(10):
            await pilot.pause()


async def test_auth_failure_stays_on_screen(monkeypatch):
    """The toast fades and can be covered when embedded; the panel message is
    the durable copy."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = ProjectsApp(client=FailingAuthClient(), config=synced_config())

    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()

        panel = app.screen.query_one(panel_module.ProjectsPanel)
        empty = panel.query_one("#empty")

        assert "1Password is locked" in str(empty.render())
        assert empty.has_class("-visible")


async def test_status_outranks_the_per_view_empty_text(monkeypatch):
    """"no done projects" would be wrong when nothing could be loaded at all."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = ProjectsApp(
        client=FailingAuthClient("token unreadable"), config=synced_config()
    )

    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()

        panel = app.screen.query_one(panel_module.ProjectsPanel)
        rendered = str(panel.query_one("#empty").render())

        assert "token unreadable" in rendered
        assert "nothing here" not in rendered


async def test_status_clears_once_projects_load(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()

    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()

        panel = app.screen.query_one(panel_module.ProjectsPanel)

        assert panel._status is None
        assert not panel.query_one("#empty").has_class("-visible")
        assert panel._projects


# ── the logo is optional, for embedding ───────────────────────────────────


async def test_logo_shows_by_default(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()

    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(5):
            await pilot.pause()
        panel = app.screen.query_one(panel_module.ProjectsPanel)

        assert panel.query("#logo")


async def test_logo_can_be_dropped_for_a_host(monkeypatch):
    """Embedded, the host's own chrome already names the app."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)

    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield panel_module.ProjectsPanel(
                client=FakeClient(), show_logo=False, id="p"
            )

    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()
        panel = app.query_one("#p", panel_module.ProjectsPanel)

        assert not panel.query("#logo")
        # The nav is still there -- and got the rows back.
        assert panel.query_one("#nav")


async def test_dropping_the_logo_survives_a_resize(monkeypatch):
    """_fit_logo runs on every resize and used to query a widget that exists."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)

    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield panel_module.ProjectsPanel(
                client=FakeClient(), show_logo=False, id="p"
            )

    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(5):
            await pilot.pause()
        # A short terminal is what makes _fit_logo want to hide the logo.
        await pilot.resize_terminal(100, 12)
        for _ in range(5):
            await pilot.pause()

        assert app.is_running


# ── dialog contract: shared with remtui ───────────────────────────────────
#
# Both apps' dialogs follow one shape, so moving between them (or meeting one
# embedded in librarian) does not mean relearning the buttons:
#
#   [secondary…] [Cancel] [Primary]     right-aligned, primary last
#   ^e Editor  esc Cancel  ^s Save      Footer, derived from BINDINGS
#
# with focus starting in the first field, and the safe option focused in a
# destructive confirm.


async def _open_edit(app, pilot):
    for _ in range(10):
        await pilot.pause()
    panel = app.screen.query_one(panel_module.ProjectsPanel)
    panel.query_one("#projects").focus()
    for _ in range(4):
        await pilot.pause()
    await pilot.press("e")
    for _ in range(12):
        await pilot.pause()
    return app.screen


async def test_edit_dialog_button_order_and_ids(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        buttons = [(b.id, str(b.label)) for b in modal.query("Button")]
        assert buttons == [
            ("btn-editor", "Editor"),
            ("btn-done", "Done"),
            ("btn-cancel", "Cancel"),
            ("btn-save", "Save"),
        ]


async def test_edit_dialog_buttons_are_right_aligned(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        row = modal.query_one(".button-row")
        assert row.styles.align_horizontal == "right"


async def test_edit_dialog_labels_carry_no_shortcut_text(monkeypatch):
    """The Footer owns the hints, so labels cannot drift from the bindings."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        for button in modal.query("Button"):
            label = str(button.label)
            assert "^" not in label and "Ctrl" not in label and "(" not in label
            # And no hardcoded editor name: the action opens $EDITOR.
            assert "Vim" not in label


async def test_edit_dialog_has_a_footer_listing_its_shortcuts(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        assert modal.query("Footer"), "the dialog should carry its own Footer"

        shown = {
            key: ab.binding.description
            for key, ab in modal.active_bindings.items()
            if ab.binding.show
        }
        assert shown["ctrl+s"] == "Save"
        assert shown["ctrl+e"] == "Editor"
        assert shown["ctrl+d"] == "Done"
        assert shown["escape"] == "Cancel"


async def test_edit_dialog_starts_in_the_first_field(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await _open_edit(app, pilot)

        assert app.focused is not None
        assert app.focused.id == "title-input"


async def test_shortcuts_reach_the_dialog_from_inside_a_field(monkeypatch):
    """The bug priority=True fixes.

    An Input binds ctrl+e and friends itself, and a focused widget is checked
    before the screen -- so the dialog's shortcuts used to die the moment the
    cursor was in a field, which is where the dialog puts it.
    """
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)
        from textual.widgets import Input

        assert isinstance(app.focused, Input)

        for key in ("ctrl+s", "ctrl+e", "ctrl+d", "escape"):
            binding = modal.active_bindings.get(key)
            assert binding is not None, f"{key} unreachable"
            assert binding.node is modal, f"{key} is being eaten by {binding.node!r}"


async def test_ctrl_d_fires_with_the_cursor_in_a_field(monkeypatch):
    """Functional half of the above: the key actually dispatches."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        fired = []
        monkeypatch.setattr(
            type(modal), "action_toggle_complete", lambda self: fired.append(1)
        )
        await pilot.press("ctrl+d")
        for _ in range(4):
            await pilot.pause()

        assert fired, "ctrl+d did not reach the dialog"


async def test_the_form_scrolls_rather_than_hiding_a_field(monkeypatch):
    """The docked button row used to render on top of the last field."""
    from textual.containers import VerticalScroll

    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        form = modal.query_one(".form-container")
        assert isinstance(form, VerticalScroll)

        row = modal.query_one(".button-row")
        # The button row starts at or below where the form ends.
        assert row.region.y >= form.region.y + form.region.height - 1


async def test_tab_walks_fields_then_buttons_ending_on_the_primary(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        seen = []
        for _ in range(24):
            await pilot.press("tab")
            await pilot.pause()
            if app.focused is not None:
                seen.append(app.focused.id)
            if seen[-1:] == ["btn-save"]:
                break

        buttons = [i for i in seen if i and i.startswith("btn-")]
        assert buttons == ["btn-editor", "btn-done", "btn-cancel", "btn-save"], seen
        # Every button comes after the fields.
        assert seen.index("btn-editor") > seen.index("note-text")


async def test_delete_confirm_focuses_the_safe_option(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        for _ in range(10):
            await pilot.pause()
        panel = app.screen.query_one(panel_module.ProjectsPanel)
        panel.query_one("#projects").focus()
        for _ in range(4):
            await pilot.pause()
        await pilot.press("d")
        for _ in range(12):
            await pilot.pause()

        modal = app.screen
        assert type(modal).__name__ == "ConfirmDeleteModal"
        buttons = [(b.id, str(b.label)) for b in modal.query("Button")]
        assert buttons == [("btn-cancel", "Cancel"), ("btn-delete", "Delete")]
        assert app.focused.id == "btn-cancel"
        assert modal.query("Footer")


async def test_button_row_and_footer_do_not_overlap(monkeypatch):
    """Both were docked bottom, so the footer clipped the buttons' last row."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        modal = await _open_edit(app, pilot)

        row = modal.query_one(".button-row").region
        footer = modal.query_one("Footer").region

        assert row.height == 3, "buttons should not be squashed"
        assert row.y + row.height <= footer.y, (
            f"button row {row} overlaps footer {footer}"
        )


# ==================== The transient Conflicts row ====================


def _conflicted(project):
    from projection.models import FieldConflict

    project.conflicts["note"] = FieldConflict(
        backend="smartsheet", mine="mine", theirs="theirs", base="agreed"
    )
    return project


async def test_the_conflicts_row_is_absent_when_there_are_none(monkeypatch):
    """A permanent "Conflicts 0" would be clutter."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        nav = app.query_one("#nav", OptionList)
        with pytest.raises(Exception):
            nav.get_option_index("view:conflicts")


async def test_the_conflicts_row_appears_when_a_project_has_one(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.panel
        _conflicted(panel._projects[0])
        panel._build_nav()
        await pilot.pause()

        nav = app.query_one("#nav", OptionList)
        assert nav.get_option_index("view:conflicts") is not None


async def test_the_conflicts_view_lists_only_conflicted_projects(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.panel
        _conflicted(panel._projects[1])
        panel._build_nav()
        await pilot.pause()

        nav = app.query_one("#nav", OptionList)
        nav.highlighted = nav.get_option_index("view:conflicts")
        await pilot.pause()
        listing = app.query_one("#projects", ListView)
        assert len(listing) == 1
        assert panel._selected_project().title == "AI Assistant"


async def test_leaving_the_conflicts_view_when_it_empties(monkeypatch):
    """The row disappears; the pane must not be left pointed at nothing."""
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.panel
        project = _conflicted(panel._projects[0])
        panel._build_nav()
        await pilot.pause()
        nav = app.query_one("#nav", OptionList)
        nav.highlighted = nav.get_option_index("view:conflicts")
        await pilot.pause()
        assert panel.view_kind == "conflicts"

        project.conflicts.clear()
        panel._build_nav()
        await pilot.pause()
        assert panel.view_kind == "all"


async def test_a_conflicted_row_shows_a_warning_badge(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    panel = make_panel()
    project = _conflicted(_project("ZTNA", 1, status="In progress"))
    body = ProjectItem(project)._body().plain
    assert "⚠1" in body


async def test_c_opens_the_conflict_chooser(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.panel
        # The starred project sorts first, so this is the row at index 0.
        _conflicted(panel._projects[1])
        panel.repopulate()
        for _ in range(4):
            await pilot.pause()
        listing = app.query_one("#projects", ListView)
        listing.focus()
        listing.index = 0
        await pilot.pause()
        assert panel._selected_project().has_conflicts
        await pilot.press("c")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConflictModal"


async def test_c_on_a_clean_project_opens_nothing(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#projects", ListView).focus()
        await pilot.press("c")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
