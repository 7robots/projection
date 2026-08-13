"""Configuration for Projection.

Read once from `~/.config/projection/config.toml` (XDG) into a `Config` object
that is handed to the components that need it. Nothing here is a module-level
constant derived from the file, deliberately: a setup wizard has to be able to
write config.toml and reload it, tests have to be able to build a config
without touching the user's, and per-backend settings need somewhere to live
that is not a flat namespace of globals.

The status display tables at the bottom are *not* config — they are
presentation constants with no TOML keys behind them, so they stay module-level
and `models.py` / `widgets.py` import them directly.

A value that cannot be used does not raise. It falls back to the built-in
default and is recorded in `Config.load_error`, which the panel announces at
startup: constructing a config happens inside `compose()` when the panel is
embedded in another app, where an exception would take out the whole modal
rather than one setting. Silence is not an option either — falling back to a
built-in *sheet id* without saying so means reading someone else's sheet.

There is deliberately no `.env` loading here. The Smartsheet API token comes
from 1Password (see `secrets.py`); a token file on disk would silently outrank
it and keep working after the credential was rotated.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .columns import CANONICAL, SMARTSHEET_LEGACY
from .hooks import INPUT_CHOICES, MODE_FIRE, MODES, Hook

# --- XDG paths ---
#
# `$XDG_CONFIG_HOME` / `$XDG_DATA_HOME` when set, else the usual defaults. The
# docstring above claimed XDG for a long time while only ever reading
# `~/.config`, which made projection the odd one out among librarian, remtui and
# taskpapertui — they all resolve it this way, and the point of that is that the
# four sit side by side.
CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "projection"
)
DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    / "projection"
)

DEFAULT_CONFIG_FILE = CONFIG_DIR / "config.toml"

# Backend names, spelled out rather than imported: a backend imports `config`, so
# `config` must not import a backend. `local_storage` duplicates "smartsheet" for
# the same reason, and a test pins each against the backend that defines it.
SMARTSHEET_BACKEND = "smartsheet"
D1_BACKEND = "d1"


@dataclass(frozen=True)
class SmartsheetConfig:
    """Which sheet to read and write, and that sheet's column vocabulary.

    Nothing here has a default that names a real target: no sheet id, and no
    1Password reference. Both used to, and both were the same mistake — a tool
    other people install must not carry one team's sheet or one person's vault
    layout. Setup writes these keys explicitly instead.
    """

    # No default sheet, deliberately. A built-in id here is how a tool ends up
    # shipping one team's schema — and how a config that never named a sheet
    # silently read someone else's. Unset means "not configured", which
    # `build_backend` reports rather than guessing.
    projects_sheet_id: int = 0
    projects_sheet_name: str = ""
    # Canonical field name -> this sheet's column title. Canonical by default;
    # the older flat `[smartsheet]` spelling implies the Team Projects
    # vocabulary instead, since that section only ever described that sheet.
    columns: dict[str, str] = field(default_factory=lambda: dict(CANONICAL))
    # `op://vault/item/field` for the API token. Empty means the environment
    # variable is the only source: the package carries no default reference, since
    # one would name a particular person's vault and item.
    token_ref: str = ""


@dataclass(frozen=True)
class D1Config:
    """Which Cloudflare D1 database and table to sync with.

    No defaults for the ids: they name a write target, and a stale or guessed one
    writes into someone else's database. The table name has a default because
    Projection owns that table — its columns are Projection's, not adopted.
    """

    account_id: str = ""
    database_id: str = ""
    database_name: str = ""
    table: str = "projects"
    # `op://vault/item/field` for the Cloudflare API token. Empty means the
    # environment variable is the only source — there is no sensible default,
    # since nobody else's vault layout is knowable.
    token_ref: str = ""


@dataclass(frozen=True)
class SyncConfig:
    """How often to reconcile with the backend."""

    # Seconds between background polls; 0 disables it (manual `r` refresh only).
    # It used to live in [headless], which was never where it belonged — that
    # section described the exec summary, which is now a user script.
    poll_interval: float = 0.0


@dataclass(frozen=True)
class KeysConfig:
    """Key profile and per-binding overrides."""

    profile: str = "default"
    # Binding id -> key(s), e.g. {"project.new": "w"}. An override replaces the
    # binding's keys entirely; comma-separate to keep several.
    overrides: dict[str, str] = field(default_factory=dict)

    @property
    def vim(self) -> bool:
        """Whether the vim motion extras are active."""
        return self.profile == "vim"


# Statuses offered for new and edited projects. Must match the sheet's
# picklist, which rejects values outside its own option list.
DEFAULT_STATUS_OPTIONS: tuple[str, ...] = (
    "Not started",
    "In progress",
    "Blocked",
    "On Hold",
    "Done",
)


@dataclass(frozen=True)
class Config:
    """Everything Projection reads from config.toml."""

    # Which backend to sync with, or "" for local-only. The local store is the
    # source of record, so no backend is a valid — and the default — choice.
    backend: str = ""
    smartsheet: SmartsheetConfig = field(default_factory=SmartsheetConfig)
    d1: D1Config = field(default_factory=D1Config)
    sync: SyncConfig = field(default_factory=SyncConfig)
    keys: KeysConfig = field(default_factory=KeysConfig)
    # User scripts bound to keys. Empty by default: what to *do* with projects is
    # the user's business, and nothing here ships with an opinion about it.
    hooks: tuple[Hook, ...] = ()
    status_options: tuple[str, ...] = DEFAULT_STATUS_OPTIONS
    # A factory, not `= DATA_DIR`: a bare default is bound when the class is
    # defined, so a test that redirects DATA_DIR would still get the real one.
    data_dir: Path = field(default_factory=lambda: DATA_DIR)
    # Where this configuration was read from, so `save()` writes back to the
    # same file. Without it, setup inside a test — or against a `--config`
    # override — would quietly rewrite the real config.toml.
    source: Path | None = None
    # Why one or more settings fell back to a built-in default, or None if the
    # file was read cleanly. Surfaced at startup rather than swallowed.
    load_error: str | None = None
    # True when this load had to create config.toml — a genuine first run, and
    # the one moment offering the setup wizard unprompted is welcome rather than
    # intrusive. Not a setting: there is no TOML key behind it, and it is False
    # on every subsequent launch.
    first_run: bool = False

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        create_missing: bool = True,
    ) -> Config:
        """Read config.toml, writing a commented default file if absent."""
        file_path = path or DEFAULT_CONFIG_FILE
        problems: list[str] = []
        toml, created = _read_toml(
            file_path, create_missing=create_missing, problems=problems
        )

        smartsheet, smartsheet_configured = _smartsheet(toml, problems)

        sync = SyncConfig(
            poll_interval=_pick_float(
                toml,
                (("sync", "poll_interval"), ("headless", "poll_interval")),
                SyncConfig().poll_interval,
                problems,
            )
        )

        d1 = _d1(toml, problems)

        return cls(
            backend=_backend(toml, smartsheet_configured, problems),
            smartsheet=smartsheet,
            d1=d1,
            sync=sync,
            keys=_keys(toml, problems),
            hooks=_hooks(toml, problems),
            status_options=_status_options(toml, problems),
            load_error="; ".join(problems) or None,
            first_run=created,
            source=file_path,
        )

    # ==================== Per-backend settings ====================
    #
    # Setup reads a backend's settings as a flat mapping, renders a field per
    # entry, and hands the answers back. Both directions live here, next to each
    # other and next to the dataclasses they map: they are one mapping, and split
    # across two modules they drift apart silently.

    def backend_values(self, backend: str) -> dict[str, Any]:
        """This backend's settings as plain values, for a setup form."""
        if backend == SMARTSHEET_BACKEND:
            return {
                "sheet_id": self.smartsheet.projects_sheet_id or "",
                "sheet_name": self.smartsheet.projects_sheet_name,
                "token_ref": self.smartsheet.token_ref,
            }
        if backend == D1_BACKEND:
            return {
                "account_id": self.d1.account_id,
                "database_id": self.d1.database_id,
                "database_name": self.d1.database_name,
                "table": self.d1.table,
                "token_ref": self.d1.token_ref,
            }
        return {}

    def with_provisioned_target(
        self, backend: str, target_id: str, name: str = ""
    ) -> Config:
        """A copy naming the target a `provision()` just created.

        Which keys hold an id and a name is per backend, and this file is where
        that is already written down — so a caller does not have to choose between
        `sheet_id` and `database_id`.
        """
        if backend == SMARTSHEET_BACKEND:
            try:
                sheet_id = int(target_id)
            except (TypeError, ValueError):
                raise ValueError(f"{target_id!r} is not a Smartsheet sheet id")
            return dataclasses.replace(
                self,
                smartsheet=dataclasses.replace(
                    self.smartsheet,
                    projects_sheet_id=sheet_id,
                    projects_sheet_name=name or self.smartsheet.projects_sheet_name,
                ),
            )
        if backend == D1_BACKEND:
            return dataclasses.replace(
                self,
                d1=dataclasses.replace(
                    self.d1,
                    database_id=str(target_id),
                    database_name=name or self.d1.database_name,
                ),
            )
        return self

    def backend_columns(self, backend: str) -> dict[str, str]:
        """This backend's canonical-field -> column-title map.

        Only a backend that *adopts* someone else's structure has one. A D1 table
        is Projection's own, so its columns are the canonical names and there is
        nothing to map.
        """
        if backend == SMARTSHEET_BACKEND:
            return dict(self.smartsheet.columns)
        return dict(CANONICAL)

    def with_backend_values(
        self,
        backend: str,
        values: dict[str, Any],
        *,
        columns: dict[str, str] | None = None,
    ) -> Config:
        """A copy with one backend's settings replaced by a form's answers.

        Only the named backend's block changes. The other's is left exactly as it
        was, so switching backends — or turning one off — never means re-entering
        ids you had already given.
        """
        def text(key: str, default: str) -> str:
            raw = values.get(key)
            return default if raw is None else str(raw).strip()

        if backend == SMARTSHEET_BACKEND:
            raw_id = values.get("sheet_id")
            try:
                sheet_id = int(raw_id) if str(raw_id or "").strip() else 0
            except (TypeError, ValueError):
                sheet_id = 0
            return dataclasses.replace(
                self,
                smartsheet=SmartsheetConfig(
                    projects_sheet_id=sheet_id,
                    projects_sheet_name=text(
                        "sheet_name", self.smartsheet.projects_sheet_name
                    ),
                    columns=dict(columns or self.smartsheet.columns),
                    token_ref=text("token_ref", self.smartsheet.token_ref),
                ),
            )
        if backend == D1_BACKEND:
            return dataclasses.replace(
                self,
                d1=D1Config(
                    account_id=text("account_id", self.d1.account_id),
                    database_id=text("database_id", self.d1.database_id),
                    database_name=text("database_name", self.d1.database_name),
                    table=text("table", self.d1.table) or D1Config().table,
                    token_ref=text("token_ref", self.d1.token_ref),
                ),
            )
        return self

    # ==================== Writing ====================

    def save(self, path: Path | None = None) -> Path:
        """Write these settings to config.toml, keeping the old file as `.bak`.

        The file is **regenerated** from this object rather than patched in
        place, so what lands on disk is exactly what the next `load()` reads
        back — the property setup depends on, since it writes a config and then
        rebuilds the backend from the reloaded one. The cost is that comments a
        user hand-wrote are not carried over, which is why the previous file is
        copied aside first instead of simply being replaced.

        The write is atomic: a half-written config.toml parses as "all
        defaults", and for `backend` that means silently going local-only.
        """
        file_path = path or self.source or DEFAULT_CONFIG_FILE
        # Write through a symlink, not over it. The atomic write below is
        # `os.replace`, which swaps the *link* for the temp file — so a config
        # symlinked out of a dotfiles repo silently stops syncing the first time
        # anything saves, and the repo keeps serving a stale copy on every other
        # machine. Resolving first means the link survives and the real file is
        # what changes.
        if file_path.is_symlink():
            file_path = file_path.resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.exists():
            shutil.copy2(file_path, file_path.with_name(file_path.name + ".bak"))

        fd, tmp = tempfile.mkstemp(dir=file_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self.to_toml())
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, file_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return file_path

    def to_toml(self) -> str:
        """These settings as the text of a config.toml."""
        lines: list[str] = [
            "# Projection configuration.",
            "#",
            "# The JSON store in the data directory is the source of record, so every",
            "# setting here is optional. Written by Projection's setup; edit freely.",
            "",
            "# Which backend to mirror to. Empty means local-only.",
            f"backend = {_toml_str(self.backend)}",
            "",
        ]

        sheet = self.smartsheet
        # Written whenever a sheet is named, even for `backend = ""`: turning the
        # integration off should not throw away which sheet it pointed at. The
        # same goes for the D1 table below — one backend is active at a time, and
        # switching between them should not mean re-entering ids.
        if sheet.projects_sheet_id:
            lines += [
                "[backends.smartsheet]",
                f"sheet_id = {sheet.projects_sheet_id}",
            ]
            if sheet.projects_sheet_name:
                lines.append(f"sheet_name = {_toml_str(sheet.projects_sheet_name)}")
            if sheet.token_ref:
                lines.append(f"token_ref = {_toml_str(sheet.token_ref)}")
            lines.append("")
            # Only when the sheet has its own vocabulary. A sheet Projection
            # provisioned carries the canonical titles, and writing a table that
            # restates them would suggest there is something to keep in step.
            custom = {
                name: title
                for name, title in sheet.columns.items()
                if CANONICAL.get(name) != title
            }
            if custom:
                lines.append("# This sheet's own column titles.")
                lines.append("[backends.smartsheet.columns]")
                lines += [
                    f"{name} = {_toml_str(sheet.columns[name])}"
                    for name in CANONICAL
                    if name in custom
                ]
                lines.append("")

        d1 = self.d1
        if d1.account_id or d1.database_id:
            lines.append("[backends.d1]")
            for key, value in (
                ("account_id", d1.account_id),
                ("database_id", d1.database_id),
                ("database_name", d1.database_name),
                ("token_ref", d1.token_ref),
            ):
                if value:
                    lines.append(f"{key} = {_toml_str(value)}")
            # Written even at its default: this names the table every write goes
            # to, and knowing which one without opening the source is worth a line.
            lines += [f"table = {_toml_str(d1.table)}", ""]

        lines += [
            "[sync]",
            "# Seconds between background polls of the backend. 0 means manual `r` only.",
            f"poll_interval = {self.sync.poll_interval:g}",
            "",
            "[status_options]",
            "projects = [" + ", ".join(_toml_str(s) for s in self.status_options) + "]",
            "",
            "[keys]",
            f"profile = {_toml_str(self.keys.profile)}",
        ]
        lines += [
            f"{_toml_key(binding)} = {_toml_str(keys)}"
            for binding, keys in sorted(self.keys.overrides.items())
        ]
        lines.append("")

        for hook in self.hooks:
            lines += ["[[hooks]]", f"id = {_toml_str(hook.id)}"]
            if hook.label:
                lines.append(f"label = {_toml_str(hook.label)}")
            if hook.key:
                lines.append(f"key = {_toml_str(hook.key)}")
            command = ", ".join(_toml_str(part) for part in hook.command)
            lines += [
                f"command = [{command}]",
                f"input = {_toml_str(hook.input)}",
                f"mode = {_toml_str(hook.mode)}",
                f"timeout = {hook.timeout:g}",
            ]
            if hook.env:
                lines.append(
                    "env = [" + ", ".join(_toml_str(name) for name in hook.env) + "]"
                )
            if hook.review_title:
                lines.append(f"review_title = {_toml_str(hook.review_title)}")
            lines.append("")

        return "\n".join(lines).rstrip("\n") + "\n"


# ==================== TOML writing ====================


def _toml_str(value: str) -> str:
    """A string as a TOML basic string.

    `json.dumps` is the escaper because every escape it emits (`\\"`, `\\\\`,
    `\\n`, `\\uXXXX` for anything non-ASCII) is also valid inside a TOML basic
    string — and hand-rolling this is how a folder name with a quote in it ends
    up producing a file that no longer parses.
    """
    return json.dumps(value)


def _toml_key(name: str) -> str:
    """A table key, quoted when it is not a bare key.

    Binding ids contain dots (`project.new`), which unquoted would nest tables.
    """
    if name and all(c.isalnum() or c in "-_" for c in name):
        return name
    return json.dumps(name)


# ==================== TOML reading ====================


def _read_toml(
    path: Path, *, create_missing: bool, problems: list[str]
) -> tuple[dict, bool]:
    """The parsed file plus whether this call had to create it.

    An unreadable file returns an empty mapping — "all defaults" — and is *not*
    reported as a first run: it has content, and offering to overwrite it would
    be the wrong response to a syntax error.
    """
    if not path.exists():
        created = False
        if create_missing:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(DEFAULT_TOML)
                created = True
            except OSError as e:
                problems.append(f"could not create {path} ({e})")
        else:
            # Nothing on disk and nothing written: still a first run as far as
            # the caller is concerned, since no settings exist yet.
            created = True
        # The file just written *is* the defaults, so there is nothing to read
        # back — an empty mapping resolves every key to its built-in value.
        return {}, created

    try:
        with open(path, "rb") as f:
            loaded = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        problems.append(f"{path} could not be read ({e}) — using built-in defaults")
        return {}, False

    return (loaded if isinstance(loaded, dict) else {}), False


def _value(toml: dict, section: str, key: str) -> Any:
    """Raw value for a key, or None if the section or key is absent."""
    section_data = toml.get(section)
    if not isinstance(section_data, dict):
        return None
    return section_data.get(key)


def _str(toml: dict, section: str, key: str, default: str, problems: list[str]) -> str:
    raw = _value(toml, section, key)
    if raw is None:
        return default
    if not isinstance(raw, str):
        problems.append(
            f"[{section}] {key} = {raw!r} is not text — using {default!r}"
        )
        return default
    return raw


def _int(toml: dict, section: str, key: str, default: int, problems: list[str]) -> int:
    raw = _value(toml, section, key)
    if raw is None:
        return default
    # bool is an int subclass, and `projects_sheet_id = true` is never meant.
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        problems.append(
            f"[{section}] {key} = {raw!r} is not a whole number — using {default}"
        )
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        problems.append(
            f"[{section}] {key} = {raw!r} is not a whole number — using {default}"
        )
        return default


def _float(
    toml: dict, section: str, key: str, default: float, problems: list[str]
) -> float:
    raw = _value(toml, section, key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        problems.append(
            f"[{section}] {key} = {raw!r} is not a number — using {default}"
        )
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        problems.append(
            f"[{section}] {key} = {raw!r} is not a number — using {default}"
        )
        return default


def _status_options(toml: dict, problems: list[str]) -> tuple[str, ...]:
    """Configured statuses first, then any built-in the file left out.

    A stale config.toml that predates a new status should not hide it, but the
    user's ordering is theirs to choose.
    """
    raw = _value(toml, "status_options", "projects")
    statuses: list[str] = []
    if raw is not None:
        if isinstance(raw, list) and all(isinstance(s, str) for s in raw):
            statuses = list(raw)
        else:
            problems.append(
                "[status_options] projects must be a list of strings — "
                "using the built-in statuses"
            )
    statuses += [s for s in DEFAULT_STATUS_OPTIONS if s not in statuses]
    return tuple(statuses)


def _pick_float(
    toml: dict,
    places: tuple[tuple[str, str], ...],
    default: float,
    problems: list[str],
) -> float:
    """First present value across several (section, key) spellings."""
    for section, key in places:
        if _value(toml, section, key) is not None:
            return _float(toml, section, key, default, problems)
    return default


def _table(toml: dict, *path: str) -> Optional[dict]:
    """A nested table (`[a.b.c]`), or None if any level is absent or not a table."""
    node: Any = toml
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None


def _pick(
    tables: tuple[dict, ...], keys: tuple[str, ...]
) -> tuple[Any, str]:
    """First present value for any of `keys`, and the key it was found under.

    The key comes back so a complaint can name what the user actually typed —
    telling someone `sheet_id` is wrong when their file says
    `projects_sheet_id` sends them looking for a line that isn't there.
    """
    for table in tables:
        for key in keys:
            if key in table:
                return table[key], key
    return None, keys[0]


def _smartsheet(toml: dict, problems: list[str]) -> tuple[SmartsheetConfig, bool]:
    """Read the Smartsheet backend's settings from either config layout.

    `[backends.smartsheet]` is the current spelling; the older flat
    `[smartsheet]` section is still honoured so existing installs keep working.
    The two differ in one important way: the older section only ever described
    the Team Projects sheet, so it implies **that sheet's** column names, while
    the newer one defaults to canonical names — which is what a sheet Projection
    provisions itself will have.

    The old `summary_*` keys are no longer read at all: the exec summary is a
    user script now, and reads its own config. Leaving them in config.toml is
    harmless.
    """
    modern = _table(toml, "backends", "smartsheet") or {}
    legacy = toml.get("smartsheet")
    legacy = legacy if isinstance(legacy, dict) else {}
    tables = (modern, legacy)
    defaults = SmartsheetConfig()

    def as_int(keys: tuple[str, ...], default: int) -> int:
        raw, found = _pick(tables, keys)
        if raw is None:
            return default
        complaint = f"{found} = {raw!r} is not a whole number — using {default}"
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            problems.append(complaint)
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            problems.append(complaint)
            return default

    def as_str(keys: tuple[str, ...], default: str) -> str:
        raw, found = _pick(tables, keys)
        if raw is None:
            return default
        if not isinstance(raw, str):
            problems.append(f"{found} = {raw!r} is not text — using {default!r}")
            return default
        return raw

    # Legacy names only when the legacy section is actually present. Keying this
    # off "no modern section" instead would give the EA vocabulary to a config
    # with no Smartsheet settings at all.
    base_columns = dict(
        SMARTSHEET_LEGACY if (legacy and not modern) else CANONICAL
    )
    configured = _table(toml, "backends", "smartsheet", "columns")
    if configured is not None:
        for canonical, title in configured.items():
            if not isinstance(title, str):
                problems.append(
                    f"[backends.smartsheet.columns] {canonical} = {title!r} is "
                    "not a column title — ignored"
                )
                continue
            if canonical not in CANONICAL:
                problems.append(
                    f"[backends.smartsheet.columns] {canonical!r} is not a "
                    f"field ({', '.join(CANONICAL)}) — ignored"
                )
                continue
            base_columns[canonical] = title

    return (
        SmartsheetConfig(
            projects_sheet_id=as_int(
                ("sheet_id", "projects_sheet_id"), defaults.projects_sheet_id
            ),
            projects_sheet_name=as_str(
                ("sheet_name", "projects_sheet_name"), defaults.projects_sheet_name
            ),
            columns=base_columns,
            token_ref=as_str(("token_ref",), defaults.token_ref),
        ),
        # Only a projects-sheet key counts: a leftover section carrying nothing
        # but the old summary_* keys does not mean "sync with Smartsheet".
        any(
            key in table
            for table in (modern, legacy)
            for key in ("sheet_id", "projects_sheet_id")
        ),
    )


def _d1(toml: dict, problems: list[str]) -> D1Config:
    """Read `[backends.d1]`. One spelling only — this table is new."""
    table = _table(toml, "backends", "d1") or {}
    defaults = D1Config()

    def as_str(key: str, default: str) -> str:
        raw = table.get(key)
        if raw is None:
            return default
        if not isinstance(raw, str):
            problems.append(
                f"[backends.d1] {key} = {raw!r} is not text — using {default!r}"
            )
            return default
        return raw.strip()

    return D1Config(
        account_id=as_str("account_id", defaults.account_id),
        database_id=as_str("database_id", defaults.database_id),
        database_name=as_str("database_name", defaults.database_name),
        table=as_str("table", defaults.table) or defaults.table,
        token_ref=as_str("token_ref", defaults.token_ref),
    )


def _backend(toml: dict, smartsheet_configured: bool, problems: list[str]) -> str:
    """Which backend to sync with.

    An explicit `backend = "..."` wins. Failing that, a config that carries
    Smartsheet settings means Smartsheet — so an install predating this key keeps
    syncing rather than silently going local-only. A config with neither is
    local-only, which is the intended default for a fresh install.
    """
    raw = toml.get("backend")
    if raw is not None:
        if not isinstance(raw, str):
            problems.append(f"backend = {raw!r} is not text — using local-only")
            return ""
        return raw.strip().lower()
    return "smartsheet" if smartsheet_configured else ""


def _hooks(toml: dict, problems: list[str]) -> tuple[Hook, ...]:
    """Read `[[hooks]]`.

    A malformed hook is skipped and reported rather than raising: one bad entry
    should cost you that hook, not the application.
    """
    raw = toml.get("hooks")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("[[hooks]] must be a list of tables — ignored")
        return ()

    hooks: list[Hook] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"[[hooks]] #{index + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not a table — ignored")
            continue

        hook_id = entry.get("id")
        if not isinstance(hook_id, str) or not hook_id.strip():
            problems.append(f"{where} needs a text `id` — ignored")
            continue
        hook_id = hook_id.strip()
        if hook_id in seen:
            problems.append(f"{where} repeats id {hook_id!r} — ignored")
            continue

        command = entry.get("command")
        if isinstance(command, str):
            # A bare string is the one shape that looks obviously right and is
            # wrong: it would have to be split, and splitting is shell parsing.
            problems.append(
                f"{where} `command` must be a list of arguments, not one "
                f"string — ignored"
            )
            continue
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            problems.append(
                f"{where} needs `command` as a non-empty list of strings — ignored"
            )
            continue
        # Only the program is path-expanded; arguments are passed through as-is.
        resolved = (str(Path(command[0]).expanduser()), *command[1:])

        source = entry.get("input", "all")
        if source not in INPUT_CHOICES:
            problems.append(
                f"{where} `input` = {source!r} is not one of "
                f"{', '.join(INPUT_CHOICES)} — using 'all'"
            )
            source = "all"

        mode = entry.get("mode", MODE_FIRE)
        if mode not in MODES:
            problems.append(
                f"{where} `mode` = {mode!r} is not one of {', '.join(MODES)} "
                f"— using {MODE_FIRE!r}"
            )
            mode = MODE_FIRE

        timeout = entry.get("timeout", 120)
        try:
            timeout = float(timeout)
            if timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            problems.append(f"{where} `timeout` = {timeout!r} is not a positive number — using 120")
            timeout = 120.0

        env = entry.get("env", [])
        if not isinstance(env, list) or not all(isinstance(n, str) for n in env):
            problems.append(f"{where} `env` must be a list of variable names — ignored")
            env = []

        key = entry.get("key", "")
        if not isinstance(key, str):
            problems.append(f"{where} `key` = {key!r} is not text — no key bound")
            key = ""

        label = entry.get("label", "")
        if not isinstance(label, str):
            label = ""
        review_title = entry.get("review_title", "")
        if not isinstance(review_title, str):
            review_title = ""

        seen.add(hook_id)
        hooks.append(
            Hook(
                id=hook_id,
                command=resolved,
                label=label.strip(),
                key=key.strip(),
                input=source,
                mode=mode,
                timeout=timeout,
                env=tuple(env),
                review_title=review_title.strip(),
            )
        )

    return tuple(hooks)


def _keys(toml: dict, problems: list[str]) -> KeysConfig:
    raw = toml.get("keys")
    if not isinstance(raw, dict):
        return KeysConfig()

    profile = raw.get("profile", "default")
    if not isinstance(profile, str):
        problems.append(f"[keys] profile = {profile!r} is not text — using 'default'")
        profile = "default"

    # Everything else in the section is a binding-id override. Non-string
    # values are skipped rather than reported: a key id is not enumerable here,
    # so a typo cannot be told from an override for a binding we don't know.
    overrides = {
        k: v for k, v in raw.items() if k != "profile" and isinstance(v, str)
    }
    return KeysConfig(profile=profile, overrides=overrides)


DEFAULT_TOML = """\
# Projection configuration.
#
# The JSON store in ~/.local/share/projection is the source of record, so every
# setting here is optional and Projection is fully usable with none of them.

# Which backend to mirror to. Empty means local-only.
backend = ""

# Uncomment to sync with a Smartsheet. `columns` maps Projection's field names
# onto that sheet's own column titles, and is only needed when adopting a sheet
# that already has its own vocabulary.
#
# [backends.smartsheet]
# sheet_id = 0
# sheet_name = "My Projects"
#
# [backends.smartsheet.columns]
# title = "Project"
# note = "Update"
# starred = "Sync"

[sync]
# Seconds between background polls of the backend. 0 means manual `r` only.
poll_interval = 0

[status_options]
projects = ["Not started", "In progress", "Blocked", "On Hold", "Done"]

[keys]
# Key profile: "default", or "vim" for extra vim motions (gg, ctrl+d/u/f/b,
# ":" for the command palette, "o" for new project).
profile = "default"
# Per-binding overrides by binding id (see README), e.g.:
# "project.new" = "n"

# Hooks run your own script over the project list, on a key you choose. What to
# *do* with projects is yours; see examples/exec_summary/ for a worked one.
#
# [[hooks]]
# id = "exec-summary"
# label = "Executive summary"
# key = "x"
# command = ["~/path/to/script"]
# input = "starred"        # all | starred | selection | conflicts
# mode = "review"          # fire, or review to approve the draft first
# timeout = 240
# env = ["ANTHROPIC_API_KEY"]   # nothing ambient is forwarded by default
"""


# ==================== Status presentation ====================
#
# Re-exported from `statuses`, which imports nothing — see the note there. Not
# config: there are no TOML keys behind any of it.

from .statuses import (  # noqa: E402,F401
    DONE_STATUS,
    STATUS_COLORS,
    STATUS_ICONS,
    STATUS_RANK,
    status_color,
    status_icon,
    status_rank,
)
