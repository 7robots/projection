"""First-run setup: the wizard, provisioning, and what gets written.

The dialogs are driven through a real panel, because the parts worth pinning are
the seams between them — a choice becoming a provisioned target, a probe failure
stopping the write, and a mapping being asked for only when it is needed.
"""

import asyncio

import pytest
from textual.widgets import Input, Select

from projection import panel as panel_module
from projection.app import ProjectsApp
from projection.backends.base import (
    Capabilities,
    ProbeResult,
    ProvisionResult,
    RecordRef,
)
from projection.config import Config, SmartsheetConfig
from projection.hooks import Hook
from projection.sync import AdoptionReport
from projection.views import ColumnMapModal, SetupModal


class FakeClient:
    """A Smartsheet client that never authenticates or hits the network."""

    def __init__(self):
        self.authenticated = False

    async def ensure_ready(self):
        self.authenticated = True

    async def aclose(self):
        return None


class RefusingClient(FakeClient):
    """What a machine with no 1Password (or a locked one) looks like."""

    async def ensure_ready(self):
        self.authenticated = True
        raise RuntimeError("1Password is locked")


class FakeBackend:
    """A backend that records what setup asked of it."""

    name = "smartsheet"
    capabilities = Capabilities(can_provision=True)

    def __init__(
        self,
        *,
        probe_result=None,
        titles=("Project", "Status", "Assigned To", "Due Date", "Update", "Sync"),
    ):
        self._probe_result = probe_result or ProbeResult(
            ready=True, exists=True, detail="Sheet ready"
        )
        self._titles = titles
        self.provisioned = False
        self.authenticated = False

    async def ensure_ready(self):
        self.authenticated = True

    async def probe(self):
        return self._probe_result

    async def provision(self):
        self.provisioned = True
        return ProvisionResult(
            target_id="777", name="Projection Projects", detail="Created it"
        )

    async def target_columns(self):
        return self._titles

    async def fetch(self):
        return []

    async def create_record(self, fields, *, project_id=""):
        return RecordRef(id="1")

    async def update_record(self, remote_id, changes, *, expected_modified_at=None):
        return None

    async def delete_record(self, remote_id):
        return None

    def assignee_options(self):
        return []

    async def aclose(self):
        return None


class FakeSync:
    """A coordinator that records adoption instead of syncing."""

    instances: list["FakeSync"] = []

    def __init__(self, *args, on_event=None, remote=None, **kwargs):
        self._on_event = on_event
        self.remote = remote
        self.adopted = False
        FakeSync.instances.append(self)

    # -- what the panel asks of it -----------------------------------------
    busy = False

    @property
    def has_backend(self):
        return self.remote is not None

    @property
    def backend_name(self):
        return getattr(self.remote, "name", "")

    async def initial_sync(self):
        return []

    async def adopt(self):
        self.adopted = True
        return AdoptionReport(pushed=2)

    async def refresh(self):
        return []

    def load(self):
        return []

    def conflicted(self):
        return []

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
        return []


@pytest.fixture(autouse=True)
def fresh_fakes():
    FakeSync.instances.clear()
    yield
    FakeSync.instances.clear()


@pytest.fixture
def config_file(tmp_path):
    return tmp_path / "config.toml"


def _config(config_file, **kwargs):
    """A config that has been saved once, so it is not a first run."""
    kwargs.setdefault("first_run", False)
    config = Config(source=config_file, **kwargs)
    config.save()
    return config


def _install(monkeypatch, backend=None):
    """Route the panel's backend construction to a fake, and its sync too."""
    built = []

    def build(config, *, client=None, allow_unprovisioned=False):
        if not config.backend:
            return None
        built.append(config)
        return backend if backend is not None else FakeBackend()

    monkeypatch.setattr(panel_module, "build_backend", build)
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    return built


async def _settle(pilot, times=12):
    for _ in range(times):
        await pilot.pause()
    await asyncio.sleep(0)


# ==================== The wizard opens ====================


async def test_comma_opens_setup(monkeypatch, config_file):
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("comma")
        await _settle(pilot)
        assert isinstance(app.screen, SetupModal)


async def test_a_genuine_first_run_offers_setup_unprompted(monkeypatch, tmp_path):
    """config.toml did not exist until this launch."""
    _install(monkeypatch)
    config = Config(source=tmp_path / "config.toml", first_run=True)
    app = ProjectsApp(client=FakeClient(), config=config)

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert isinstance(app.screen, SetupModal)


async def test_a_later_run_does_not(monkeypatch, config_file):
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert not isinstance(app.screen, SetupModal)


# ==================== Creating a sheet ====================


async def _run_setup(pilot, app, *, backend="smartsheet", mode=None, sheet_id=None):
    """Open setup, fill it in, and save."""
    await pilot.press("comma")
    await _settle(pilot)
    screen = app.screen
    assert isinstance(screen, SetupModal)
    screen.query_one("#backend-select", Select).value = backend
    await pilot.pause()
    if mode is not None:
        screen.query_one("#mode-select", Select).value = mode
        await pilot.pause()
    if sheet_id is not None:
        screen.query_one("#field-smartsheet-sheet_id", Input).value = sheet_id
    await pilot.press("ctrl+s")
    await _settle(pilot)


async def test_creating_a_sheet_writes_its_id_to_config(monkeypatch, config_file):
    """A sheet created but not recorded is a sheet nobody can find again."""
    backend = FakeBackend()
    _install(monkeypatch, backend)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="create")

    assert backend.provisioned is True
    written = Config.load(config_file)
    assert written.backend == "smartsheet"
    assert written.smartsheet.projects_sheet_id == 777
    assert written.smartsheet.projects_sheet_name == "Projection Projects"


async def test_connecting_a_ready_sheet_writes_the_entered_id(
    monkeypatch, config_file
):
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="connect", sheet_id="1234567")

    assert Config.load(config_file).smartsheet.projects_sheet_id == 1234567


async def test_the_new_backend_is_adopted_rather_than_plainly_fetched(
    monkeypatch, config_file
):
    """Local work has to be pushed up; an ordinary fetch would never do it."""
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="create")
        # The coordinator is rebuilt for the new backend, and that one adopts.
        assert len(FakeSync.instances) == 2
        assert FakeSync.instances[-1].adopted is True
        assert FakeSync.instances[-1].has_backend is True


async def test_cancelling_writes_nothing(monkeypatch, config_file):
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))
    before = config_file.read_text()

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("comma")
        await _settle(pilot)
        app.screen.query_one("#backend-select", Select).value = "smartsheet"
        await pilot.press("escape")
        await _settle(pilot)

    assert config_file.read_text() == before


async def test_turning_the_backend_off_keeps_the_sheet_settings(
    monkeypatch, config_file
):
    """"Local only" is not "forget which sheet I use"."""
    _install(monkeypatch)
    config = _config(
        config_file,
        backend="smartsheet",
        smartsheet=SmartsheetConfig(projects_sheet_id=42, projects_sheet_name="Mine"),
    )
    app = ProjectsApp(client=FakeClient(), config=config)

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, backend="")

    written = Config.load(config_file)
    assert written.backend == ""
    assert written.smartsheet.projects_sheet_id == 42


async def test_setup_keeps_the_rest_of_the_config(monkeypatch, config_file):
    """Setup rewrites config.toml, so anything it does not know must survive.

    Losing a `[[hooks]]` entry to a backend change would take a user's own
    script off its key with nothing said.
    """
    _install(monkeypatch)
    hook = Hook(
        id="ia-summary",
        command=("/bin/echo", "hi"),
        label="Exec summary",
        key="x",
        input="starred",
        mode="review",
        timeout=240.0,
        env=("ANTHROPIC_API_KEY",),
    )
    config = _config(config_file, hooks=(hook,), status_options=("Cooking", "Done"))
    app = ProjectsApp(client=FakeClient(), config=config)

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="create")

    written = Config.load(config_file)
    assert written.hooks == (hook,)
    assert written.status_options[0] == "Cooking"


# ==================== Refusing to save something broken ====================


async def test_an_unreachable_sheet_is_not_written(monkeypatch, config_file):
    """A backend saved but unusable makes the *next* launch fail, too."""
    _install(
        monkeypatch,
        FakeBackend(
            probe_result=ProbeResult(
                ready=False, exists=False, detail="Smartsheet denied access (403)"
            )
        ),
    )
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))
    before = config_file.read_text()

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="connect", sheet_id="99")

    assert config_file.read_text() == before


async def test_a_sheet_with_other_column_names_asks_for_a_mapping(
    monkeypatch, config_file
):
    _install(
        monkeypatch,
        FakeBackend(
            probe_result=ProbeResult(
                ready=False,
                exists=True,
                missing_fields=("title",),
                detail="The sheet has no column for: title (Title)",
            )
        ),
    )
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="connect", sheet_id="99")
        assert isinstance(app.screen, ColumnMapModal)
        # Offers the sheet's real titles, not names to be typed.
        assert app.screen.query_one("#map-title", Select)._options


async def test_cancelling_the_mapping_writes_nothing(monkeypatch, config_file):
    _install(
        monkeypatch,
        FakeBackend(
            probe_result=ProbeResult(ready=False, exists=True, detail="no columns")
        ),
    )
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))
    before = config_file.read_text()

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="connect", sheet_id="99")
        assert isinstance(app.screen, ColumnMapModal)
        await pilot.press("escape")
        await _settle(pilot)

    assert config_file.read_text() == before


async def test_a_chosen_mapping_is_saved(monkeypatch, config_file):
    """The second probe decides, so the fake must pass it."""
    attempts = []

    class TwoProbes(FakeBackend):
        async def probe(self):
            attempts.append(1)
            if len(attempts) == 1:
                return ProbeResult(ready=False, exists=True, detail="wrong names")
            return ProbeResult(ready=True, exists=True, detail="ready")

    _install(monkeypatch, TwoProbes())
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="connect", sheet_id="99")
        screen = app.screen
        assert isinstance(screen, ColumnMapModal)
        for field, title in (
            ("title", "Project"),
            ("status", "Status"),
            ("assigned", "Assigned To"),
            ("due_date", "Due Date"),
            ("note", "Update"),
            ("starred", "Sync"),
        ):
            screen.query_one(f"#map-{field}", Select).value = title
        await pilot.press("ctrl+s")
        await _settle(pilot)

    written = Config.load(config_file)
    assert written.smartsheet.columns["note"] == "Update"
    assert written.smartsheet.columns["title"] == "Project"


# ==================== Local-only never touches 1Password ====================


async def test_local_only_startup_never_authenticates(monkeypatch, config_file):
    """The out-of-the-box path: no backend, so no credential, so no prompt.

    This used to fail outright on a machine with no `op`: the panel showed an
    auth error instead of the perfectly good local store.
    """
    _install(monkeypatch)
    client = RefusingClient()
    app = ProjectsApp(client=client, config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert client.authenticated is False
        assert app.panel._status is None


async def test_a_configured_backend_still_authenticates(monkeypatch, config_file):
    """And the *backend* is asked, not a Smartsheet client — each reads its own."""
    backend = FakeBackend()
    _install(monkeypatch, backend)
    config = _config(
        config_file,
        backend="smartsheet",
        smartsheet=SmartsheetConfig(projects_sheet_id=1),
    )
    app = ProjectsApp(client=FakeClient(), config=config)

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert backend.authenticated is True


async def test_the_empty_local_store_says_how_to_connect_a_backend(
    monkeypatch, config_file
):
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        rendered = str(app.panel.query_one("#empty").render())
        assert "connect a backend" in rendered


# ==================== The dialogs on their own ====================


class Harness(ProjectsApp):
    """An app whose only job is to host one dialog."""

    def __init__(self, screen):
        super().__init__(client=FakeClient(), config=Config())
        self._to_push = screen
        self.result = "unset"

    def on_mount(self) -> None:
        super().on_mount()
        self.push_screen(self._to_push, lambda value: setattr(self, "result", value))


async def test_a_bad_sheet_id_is_refused_in_the_dialog(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = Harness(SetupModal(Config(backend="smartsheet")))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        screen = app.screen
        screen.query_one("#mode-select", Select).value = "connect"
        await pilot.pause()
        screen.query_one("#field-smartsheet-sheet_id", Input).value = "not a number"
        await pilot.press("ctrl+s")
        await _settle(pilot)
        # Still open, and saying why.
        assert app.result == "unset"
        assert "is not a number" in str(screen.query_one("#setup-status").render())


async def test_the_mapping_refuses_two_fields_in_one_column(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = Harness(ColumnMapModal(("Project", "Status")))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        screen = app.screen
        for field in ("title", "status", "assigned", "due_date", "note", "starred"):
            screen.query_one(f"#map-{field}", Select).value = "Project"
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert app.result == "unset"
        assert "pick different columns" in str(
            screen.query_one("#map-message").render()
        )


async def test_the_mapping_refuses_an_unmapped_field(monkeypatch):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    app = Harness(ColumnMapModal(("Project", "Status")))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        screen = app.screen
        screen.query_one("#map-title", Select).value = "Project"
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert app.result == "unset"
        assert "needs one" in str(screen.query_one("#map-message").render())


# ==================== A second backend goes through the same flow =============


class FakeD1(FakeBackend):
    """A D1-shaped backend: repairable structure, no columns to map."""

    name = "d1"
    capabilities = Capabilities(supports_cas=True, can_provision=True)

    async def provision(self):
        self.provisioned = True
        return ProvisionResult(
            target_id="new-db", name="projection", detail="Created the database"
        )

    async def target_columns(self):
        return ()


async def _run_d1_setup(pilot, app, *, mode, fields=None):
    await pilot.press("comma")
    await _settle(pilot)
    screen = app.screen
    screen.query_one("#backend-select", Select).value = "d1"
    await pilot.pause()
    screen.query_one("#mode-select", Select).value = mode
    await pilot.pause()
    for key, value in (fields or {}).items():
        screen.query_one(f"#field-d1-{key}", Input).value = value
    await pilot.press("ctrl+s")
    await _settle(pilot)


async def test_the_wizard_asks_for_d1s_own_fields(monkeypatch, config_file):
    """Declared per backend, so a second one is a table entry not a branch."""
    _install(monkeypatch, FakeD1())
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("comma")
        await _settle(pilot)
        screen = app.screen
        screen.query_one("#backend-select", Select).value = "d1"
        await pilot.pause()

        # D1's fields are shown, Smartsheet's are not.
        assert screen.query_one("#row-d1-account_id").display is True
        assert screen.query_one("#row-smartsheet-sheet_id").display is False
        # Creating never asks for the id of something that already exists.
        screen.query_one("#mode-select", Select).value = "create"
        await pilot.pause()
        assert screen.query_one("#row-d1-database_id").display is False


async def test_creating_a_d1_database_writes_its_id(monkeypatch, config_file):
    backend = FakeD1()
    _install(monkeypatch, backend)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_d1_setup(
            pilot, app, mode="create", fields={"account_id": "acct-1"}
        )

    assert backend.provisioned is True
    written = Config.load(config_file)
    assert written.backend == "d1"
    assert written.d1.account_id == "acct-1"
    assert written.d1.database_id == "new-db"
    # The id lands in `database_id`, not `sheet_id`.
    assert written.smartsheet.projects_sheet_id == 0


async def test_a_missing_account_id_is_refused_before_anything_happens(
    monkeypatch, config_file
):
    backend = FakeD1()
    _install(monkeypatch, backend)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))
    before = config_file.read_text()

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_d1_setup(pilot, app, mode="create", fields={"account_id": ""})
        # Still open, and nothing was created.
        assert isinstance(app.screen, SetupModal)
        assert "Account id is needed" in str(
            app.screen.query_one("#setup-status").render()
        )

    assert backend.provisioned is False
    assert config_file.read_text() == before


async def test_a_database_with_no_table_is_repaired_rather_than_mapped(
    monkeypatch, config_file
):
    """No mapping dialog: the backend owns that table and can just create it."""
    probes = []

    class NeedsTable(FakeD1):
        async def provision(self):
            # Repairing returns the database it repaired, as the real backend
            # does — it created the missing table, not a new database.
            self.provisioned = True
            return ProvisionResult(
                target_id="db-1", name="projection", detail="Created the table"
            )

        async def probe(self):
            probes.append(1)
            if len(probes) == 1:
                return ProbeResult(
                    ready=False,
                    exists=True,
                    repairable=True,
                    detail="no 'projects' table yet",
                )
            return ProbeResult(ready=True, exists=True, detail="ready")

    backend = NeedsTable()
    _install(monkeypatch, backend)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_d1_setup(
            pilot,
            app,
            mode="connect",
            fields={"account_id": "acct-1", "database_id": "db-1"},
        )
        # Never asked the user anything.
        assert not isinstance(app.screen, ColumnMapModal)

    assert backend.provisioned is True
    written = Config.load(config_file)
    assert written.d1.database_id == "db-1"  # the connected one, not a new one


async def test_a_smartsheet_that_is_not_ready_is_never_repaired(
    monkeypatch, config_file
):
    """Adding columns to somebody's sheet is not a decision setup gets to make."""
    backend = FakeBackend(
        probe_result=ProbeResult(
            ready=False, exists=True, repairable=False, detail="wrong column names"
        )
    )
    _install(monkeypatch, backend)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await _run_setup(pilot, app, mode="connect", sheet_id="99")
        # The mapping dialog, not a provision.
        assert isinstance(app.screen, ColumnMapModal)

    assert backend.provisioned is False


# ==================== A config typo must not take the panel down ==============
#
# `build_backend` raises for an unknown backend name, a Smartsheet backend with
# no sheet id, and an unusable D1 table name. The panel builds its backend in
# `__init__`, which runs inside `compose()` when another app embeds it — so an
# exception there cost the whole modal over one mistyped key, which is precisely
# what `config.py` promises never happens.

BROKEN_CONFIGS = {
    "unknown backend": 'backend = "mysql"\n',
    "smartsheet with no sheet": 'backend = "smartsheet"\n',
    "unusable d1 table": (
        'backend = "d1"\n[backends.d1]\n'
        'account_id = "a"\ndatabase_id = "b"\ntable = "pro-jects"\n'
    ),
}


@pytest.mark.parametrize("label", sorted(BROKEN_CONFIGS))
def test_a_broken_backend_setting_leaves_the_panel_usable(label, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BROKEN_CONFIGS[label])
    config = Config.load(path)

    panel = panel_module.ProjectsPanel(client=FakeClient(), config=config)

    assert panel._backend is None, "it should fall back to local-only"
    assert panel._backend_error, "and record why"


@pytest.mark.parametrize("label", sorted(BROKEN_CONFIGS))
async def test_a_broken_backend_setting_still_mounts_and_says_why(
    label, monkeypatch, tmp_path
):
    monkeypatch.setattr(panel_module, "SyncCoordinator", FakeSync)
    path = tmp_path / "config.toml"
    path.write_text(BROKEN_CONFIGS[label])

    notes: list[str] = []
    monkeypatch.setattr(
        panel_module.ProjectsPanel,
        "notify",
        lambda self, message, **kw: notes.append(str(message)),
    )

    app = ProjectsApp(client=FakeClient(), config=Config.load(path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert app.is_running
        panel = app.panel
        assert panel._backend is None
        # The panel is genuinely usable: the empty state renders, and setup opens.
        assert panel.query_one("#empty")
        await pilot.press("comma")
        await _settle(pilot)
        assert isinstance(app.screen, SetupModal)

    assert any("local-only" in note for note in notes), notes
    assert any("," in note for note in notes), "it should point at the fix"


async def test_a_good_config_records_no_backend_error(monkeypatch, config_file):
    _install(monkeypatch)
    panel = panel_module.ProjectsPanel(
        client=FakeClient(),
        config=_config(
            config_file,
            backend="smartsheet",
            smartsheet=SmartsheetConfig(projects_sheet_id=1),
        ),
    )
    assert panel._backend is not None
    assert panel._backend_error is None


async def test_the_wizard_asks_for_a_smartsheet_credential_too(monkeypatch, config_file):
    """With no default reference in the package, setup has to be able to ask."""
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("comma")
        await _settle(pilot)
        screen = app.screen
        screen.query_one("#backend-select", Select).value = "smartsheet"
        await pilot.pause()
        assert screen.query_one("#row-smartsheet-token_ref").display is True


async def test_a_typed_reference_is_saved(monkeypatch, config_file):
    _install(monkeypatch)
    app = ProjectsApp(client=FakeClient(), config=_config(config_file))

    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("comma")
        await _settle(pilot)
        screen = app.screen
        screen.query_one("#backend-select", Select).value = "smartsheet"
        await pilot.pause()
        screen.query_one("#mode-select", Select).value = "connect"
        await pilot.pause()
        screen.query_one("#field-smartsheet-sheet_id", Input).value = "42"
        screen.query_one("#field-smartsheet-token_ref", Input).value = (
            "op://Private/sheets/token"
        )
        await pilot.press("ctrl+s")
        await _settle(pilot)

    written = Config.load(config_file)
    assert written.smartsheet.token_ref == "op://Private/sheets/token"
    assert written.smartsheet.projects_sheet_id == 42
