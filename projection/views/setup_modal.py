"""First-run setup: choosing, creating, or adopting a backend.

Two dialogs, in the order the work happens:

- `SetupModal` — which backend (or none), and whether to create a target or
  connect one that already exists. It can `Test` the answer before committing,
  which is a `probe()` behind a callback so no transport or credential lives in
  a dialog.
- `ColumnMapModal` — shown only when a connected target's columns are not named
  the way Projection names its fields. It offers the target's *real* column
  titles, so a mapping cannot be typed wrong.

Neither writes anything. Both return a plain description of what was asked for
and let the panel do it, because applying a choice means provisioning, writing
config.toml, and rebuilding the backend — none of which belongs in a dialog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Select, Static

from ..backends import D1, SMARTSHEET, ProbeResult
from ..columns import CANONICAL
from ..config import Config

# Backend choices offered, in menu order. The empty name is first and is the
# default: local-only is a supported way to run, not a failure to configure.
BACKEND_CHOICES: tuple[tuple[str, str], ...] = (
    ("Local only — no backend", ""),
    ("Smartsheet", SMARTSHEET),
    ("Cloudflare D1", D1),
)

MODE_CONNECT = "connect"
MODE_CREATE = "create"


@dataclass(frozen=True)
class BackendField:
    """One thing setup has to ask for, for one backend.

    Declared rather than hand-composed, so adding a backend is a table entry
    instead of another branch in `compose()` — which is how the Smartsheet-shaped
    first version of this dialog would have grown a second copy of itself.
    """

    # The key `Config.backend_values` / `Config.with_backend_values` use.
    key: str
    label: str
    placeholder: str = ""
    # MODE_CONNECT or MODE_CREATE when the field applies to only one of them: an
    # id identifies something that already exists, so creating never asks for it.
    when: Optional[str] = None
    required: bool = True
    # Validated as a whole number, and handed back as one.
    numeric: bool = False

    def applies(self, mode: str) -> bool:
        return self.when is None or self.when == mode


BACKEND_FORMS: dict[str, tuple[BackendField, ...]] = {
    SMARTSHEET: (
        BackendField(
            "sheet_id",
            "Sheet id",
            placeholder="from the sheet's Properties in Smartsheet",
            when=MODE_CONNECT,
            numeric=True,
        ),
        BackendField(
            "sheet_name",
            "Sheet name",
            placeholder="optional — checked against the sheet",
            required=False,
        ),
        BackendField(
            "token_ref",
            "1Password",
            placeholder="op://Vault/item/field — or export SMARTSHEET_API_KEY",
            required=False,
        ),
    ),
    D1: (
        BackendField(
            "account_id",
            "Account id",
            placeholder="Cloudflare account id — in any dashboard URL",
        ),
        BackendField(
            "database_id",
            "Database id",
            placeholder="from `wrangler d1 list`",
            when=MODE_CONNECT,
        ),
        BackendField(
            "database_name",
            "Database name",
            placeholder="optional — checked against the database",
            required=False,
        ),
        BackendField("table", "Table", placeholder="projects", required=False),
        BackendField(
            "token_ref",
            "1Password",
            placeholder="op://Vault/item/field — or export CLOUDFLARE_API_TOKEN",
            required=False,
        ),
    ),
}

# What the create-or-connect choice is called per backend. "Sheet" and "Database"
# are the words the services themselves use; calling both "target" would be
# tidier and less clear.
TARGET_WORDS: dict[str, str] = {SMARTSHEET: "Sheet", D1: "Database"}


@dataclass
class SetupChoice:
    """What the user asked setup to do. Nothing has happened yet."""

    # A backend name, or "" for local-only.
    backend: str = ""
    # Create the target rather than connect to an existing one.
    create: bool = False
    # The chosen backend's answers, keyed by `BackendField.key`.
    values: dict[str, Any] = field(default_factory=dict)
    # Canonical field -> the target's column title. Filled in by the mapping
    # dialog when a connected target has its own vocabulary.
    columns: dict[str, str] = field(default_factory=lambda: dict(CANONICAL))

    @property
    def is_local_only(self) -> bool:
        return not self.backend


class SetupModal(ModalScreen[Optional[SetupChoice]]):
    """Choose a backend, and create or connect its target."""

    # priority on all three for the reason EditModal documents: the cursor
    # starts in a field, and a focused Input is consulted before the screen — so
    # without it the dialog's own shortcuts are dead exactly where the cursor is.
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+t", "test", "Test", priority=True),
    ]

    DEFAULT_CSS = """
    SetupModal {
        align: center middle;
    }

    SetupModal > Container {
        width: 78;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    SetupModal .modal-title {
        dock: top;
        height: 3;
        padding: 1;
        text-style: bold;
        text-align: center;
        background: $primary;
        color: $text;
    }

    SetupModal .form-container {
        padding: 1;
        height: auto;
        max-height: 20;
    }

    SetupModal .blurb {
        color: $text-muted;
        margin-bottom: 1;
    }

    SetupModal .field-row {
        height: auto;
        margin-bottom: 1;
    }

    SetupModal .field-label {
        width: 14;
        height: 3;
        padding: 1 0;
    }

    SetupModal .field-input {
        width: 1fr;
    }

    SetupModal #setup-status {
        height: auto;
        margin-bottom: 1;
    }

    SetupModal .button-row {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    SetupModal Button {
        margin-left: 2;
        min-width: 12;
    }

    SetupModal Footer {
        height: 1;
        background: $panel;
    }
    """

    def __init__(
        self,
        config: Config,
        *,
        probe: Optional[Callable[[SetupChoice], Awaitable[ProbeResult]]] = None,
    ) -> None:
        """Args:
        config: the settings as they stand, so the dialog opens on them.
        probe: checks a choice against the real backend for the Test button.
            Injected rather than built here: probing needs a client and a
            credential, and a dialog should hold neither.
        """
        super().__init__()
        self._config = config
        self._probe = probe

    def compose(self) -> ComposeResult:
        with Container():
            yield Static("Projects backend", classes="modal-title")
            with VerticalScroll(classes="form-container"):
                yield Static(
                    "Projection keeps your projects in a local file, which is "
                    "always the source of record. A backend mirrors them "
                    "somewhere shared — it can be turned on or off at any time.",
                    classes="blurb",
                )
                with Horizontal(classes="field-row", id="backend-row"):
                    yield Static("Backend:", classes="field-label")
                    yield Select(
                        BACKEND_CHOICES,
                        value=self._config.backend
                        if self._config.backend
                        in {name for _, name in BACKEND_CHOICES}
                        else "",
                        allow_blank=False,
                        id="backend-select",
                        classes="field-input",
                    )
                with Horizontal(classes="field-row", id="mode-row"):
                    yield Static("Target:", classes="field-label", id="mode-label")
                    yield Select(
                        [
                            ("Connect one that already exists", MODE_CONNECT),
                            ("Create a new one for me", MODE_CREATE),
                        ],
                        value=self._initial_mode(),
                        allow_blank=False,
                        id="mode-select",
                        classes="field-input",
                    )
                # Every backend's fields are mounted and then hidden, rather than
                # remounted when the choice changes: a form that rebuilds itself
                # mid-edit loses whatever was half-typed in it.
                for backend, fields in BACKEND_FORMS.items():
                    values = self._config.backend_values(backend)
                    for spec in fields:
                        with Horizontal(
                            classes="field-row", id=f"row-{backend}-{spec.key}"
                        ):
                            yield Static(f"{spec.label}:", classes="field-label")
                            yield Input(
                                value=str(values.get(spec.key) or ""),
                                placeholder=spec.placeholder,
                                id=f"field-{backend}-{spec.key}",
                                classes="field-input",
                            )
                yield Static("", id="setup-status")
            with Horizontal(classes="button-row"):
                yield Button("Test", variant="default", id="btn-test")
                yield Button("Cancel", variant="default", id="btn-cancel")
                yield Button("Save", variant="primary", id="btn-save")
            yield Footer()

    def on_mount(self) -> None:
        self._sync_visibility()
        # First field, as every other dialog here does.
        self.query_one("#backend-select", Select).focus()

    # -- field visibility ----------------------------------------------------

    def _initial_mode(self) -> str:
        """Connect when the config already names a target, else create."""
        backend = self._config.backend
        values = self._config.backend_values(backend)
        identifier = values.get("sheet_id") or values.get("database_id")
        return MODE_CONNECT if identifier else MODE_CREATE

    def _sync_visibility(self) -> None:
        """Show only the fields the current choice actually needs."""
        backend = self._selected_backend()
        mode = self._selected_mode()
        local_only = not backend

        self.query_one("#mode-row").display = not local_only
        self.query_one("#mode-label", Static).update(
            f"{TARGET_WORDS.get(backend, 'Target')}:"
        )
        # Nothing to test when there is no target to reach.
        self.query_one("#btn-test", Button).display = not local_only

        for name, fields in BACKEND_FORMS.items():
            for spec in fields:
                self.query_one(f"#row-{name}-{spec.key}").display = (
                    name == backend and spec.applies(mode)
                )

    # -- reading the form ----------------------------------------------------

    def _selected_backend(self) -> str:
        value = self.query_one("#backend-select", Select).value
        return "" if value is Select.NULL else str(value)

    def _selected_mode(self) -> str:
        value = self.query_one("#mode-select", Select).value
        return MODE_CONNECT if value is Select.NULL else str(value)

    def _status(self, message: str, *, error: bool = False) -> None:
        widget = self.query_one("#setup-status", Static)
        widget.update(message)
        widget.styles.color = "red" if error else "auto"

    def _choice(self) -> Optional[SetupChoice]:
        """The form as a `SetupChoice`, or None if it does not make sense yet."""
        backend = self._selected_backend()
        if not backend:
            # Turning the backend off keeps its settings: they are what turning it
            # back on would need, and discarding them is not implied by
            # "local only".
            return SetupChoice(backend="")

        mode = self._selected_mode()
        creating = mode == MODE_CREATE
        values: dict[str, Any] = {}
        for spec in BACKEND_FORMS.get(backend, ()):
            if not spec.applies(mode):
                continue
            raw = self.query_one(f"#field-{backend}-{spec.key}", Input).value.strip()
            if not raw:
                if spec.required:
                    self._status(f"{spec.label} is needed.", error=True)
                    return None
                continue
            if spec.numeric:
                try:
                    number = int(raw.replace(",", ""))
                except ValueError:
                    self._status(f"{spec.label}: {raw!r} is not a number.", error=True)
                    return None
                if number <= 0:
                    self._status(f"{spec.label} is a positive number.", error=True)
                    return None
                values[spec.key] = number
            else:
                values[spec.key] = raw

        return SetupChoice(
            backend=backend,
            create=creating,
            values=values,
            # Canonical to start with. A connected target with its own column
            # titles gets a mapping from the next dialog, once its real columns
            # are known — guessing here would only produce names to correct.
            columns=(
                dict(CANONICAL)
                if creating
                else dict(self._config.backend_columns(backend))
            ),
        )

    # -- actions -------------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        self._sync_visibility()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        elif event.button.id == "btn-test":
            self.action_test()
        elif event.button.id == "btn-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        choice = self._choice()
        if choice is not None:
            self.dismiss(choice)

    def action_test(self) -> None:
        """Probe the entered target, without committing to it."""
        if not self._selected_backend():
            self._status("Local-only needs nothing to test.")
            return
        choice = self._choice()
        if choice is None:
            return
        if choice.create:
            self._status("Nothing to test yet — it is created on Save.")
            return
        if self._probe is None:
            self._status("Cannot test from here.", error=True)
            return
        self._status("◌  checking…")
        self.run_worker(self._run_probe(choice), name="setup-probe")

    async def _run_probe(self, choice: SetupChoice) -> None:
        assert self._probe is not None
        try:
            result = await self._probe(choice)
        except Exception as e:  # a backend can raise anything
            self._status(f"⚠  {e}", error=True)
            return
        if result.ready:
            self._status(f"✓  {result.detail or 'ready'}")
        else:
            self._status(f"⚠  {result.detail or 'not usable yet'}", error=True)


class ColumnMapModal(ModalScreen[Optional[dict[str, str]]]):
    """Map Projection's fields onto a sheet's own column titles."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    DEFAULT_CSS = """
    ColumnMapModal {
        align: center middle;
    }

    ColumnMapModal > Container {
        width: 78;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    ColumnMapModal .modal-title {
        dock: top;
        height: 3;
        padding: 1;
        text-style: bold;
        text-align: center;
        background: $primary;
        color: $text;
    }

    ColumnMapModal .form-container {
        padding: 1;
        height: auto;
        max-height: 24;
    }

    ColumnMapModal .blurb {
        color: $text-muted;
        margin-bottom: 1;
    }

    ColumnMapModal .field-row {
        height: auto;
        margin-bottom: 1;
    }

    ColumnMapModal .field-label {
        width: 14;
        height: 3;
        padding: 1 0;
    }

    ColumnMapModal .field-input {
        width: 1fr;
    }

    ColumnMapModal #map-message {
        height: auto;
    }

    ColumnMapModal .button-row {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    ColumnMapModal Button {
        margin-left: 2;
        min-width: 12;
    }

    ColumnMapModal Footer {
        height: 1;
        background: $panel;
    }
    """

    def __init__(
        self,
        titles: tuple[str, ...],
        columns: Optional[dict[str, str]] = None,
        *,
        target_name: str = "",
    ) -> None:
        """Args:
        titles: the columns the target actually has.
        columns: the mapping as it stands, used to preselect.
        target_name: named in the blurb, so it is clear which target this is.
        """
        super().__init__()
        self._titles = tuple(dict.fromkeys(titles))  # de-duped, order kept
        self._columns = dict(columns or CANONICAL)
        self._target_name = target_name

    def compose(self) -> ComposeResult:
        where = f" in “{self._target_name}”" if self._target_name else ""
        with Container():
            yield Static("Match the sheet's columns", classes="modal-title")
            with VerticalScroll(classes="form-container"):
                yield Static(
                    f"These columns{where} do not match Projection's field "
                    "names. Pick the column each field lives in; anything not "
                    "listed here is left untouched.",
                    classes="blurb",
                )
                for name, label in CANONICAL.items():
                    current = self._columns.get(name, "")
                    options = [(title, title) for title in self._titles]
                    with Horizontal(classes="field-row"):
                        yield Static(f"{label}:", classes="field-label")
                        yield Select(
                            options,
                            value=current if current in self._titles else Select.NULL,
                            prompt="— pick a column —",
                            id=f"map-{name}",
                            classes="field-input",
                        )
                yield Static("", id="map-message")
            with Horizontal(classes="button-row"):
                yield Button("Cancel", variant="default", id="btn-cancel")
                yield Button("Save", variant="primary", id="btn-save")
            yield Footer()

    def on_mount(self) -> None:
        first = next(iter(CANONICAL))
        self.query_one(f"#map-{first}", Select).focus()

    def _mapping(self) -> Optional[dict[str, str]]:
        """The chosen mapping, or None with a reason shown."""
        chosen: dict[str, str] = {}
        for name in CANONICAL:
            value = self.query_one(f"#map-{name}", Select).value
            if value is Select.NULL:
                self._status(
                    f"{CANONICAL[name]} has no column yet — every field needs one.",
                )
                return None
            chosen[name] = str(value)

        used: dict[str, str] = {}
        for name, title in chosen.items():
            if title in used:
                # Two fields writing one column would have each overwrite the
                # other on every save.
                self._status(
                    f"{CANONICAL[used[title]]} and {CANONICAL[name]} both point "
                    f"at {title!r} — pick different columns."
                )
                return None
            used[title] = name
        return chosen

    def _status(self, message: str) -> None:
        widget = self.query_one("#map-message", Static)
        widget.update(message)
        widget.styles.color = "red"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        mapping = self._mapping()
        if mapping is not None:
            self.dismiss(mapping)
