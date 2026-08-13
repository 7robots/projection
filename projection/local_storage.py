"""The local JSON store — Projection's source of record.

This is not a cache. Projection works with no backend configured at all, and a
backend *syncs with* this file rather than owning it. Three rules follow, and
each one is the opposite of what a cache would do:

- **Nothing is ever discarded to make a version fit.** An older layout is
  migrated. A layout this build does not recognise is moved aside intact rather
  than overwritten, and a record that fails validation is carried through the
  next save untouched instead of vanishing.
- **Identity is local.** Records are keyed by `Project.id`, which is minted once
  and never changes; a backend's key lives in `remote[backend].id`.
- **Deletes leave tombstones.** Without them a missing record is ambiguous —
  deleted here, or created on another device since the last sync? — and the next
  refresh happily resurrects it.

Writes are atomic (temp file + `os.replace()`): writing in place would leave a
truncated, unparseable file, and an unparseable store reads as *empty*.
"""

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import (
    FIELD_NAMES,
    Project,
    ProjectFields,
    RemoteLink,
    now_stamp,
    parse_stamp,
    people,
    to_iso_date,
)

# Default storage directory.
DEFAULT_STORAGE_DIR = Path.home() / ".local" / "share" / "projection"

# Store layout version.
#
#   1  title-keyed, Google Sheets era (long gone)
#   2  keyed by Smartsheet row id; local_id a placeholder until one arrived
#   3  keyed by a permanent local id, backends mapped in `remote`, per-field
#      timestamps, merge bases, tombstones
#
# Bump this only alongside a migration in `_migrate`. There is no longer a path
# that answers a version mismatch by returning nothing.
SCHEMA_VERSION = 3

# The oldest layout `_migrate` can still read.
OLDEST_MIGRATABLE = 2

# v2 stored exactly one backend's keys, before backends were named.
_V2_BACKEND = "smartsheet"

# Anything that is not plainly part of a filename. Used on the "why" in a
# moved-aside file's name, which can come from the stored file's own contents.
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]")

# How long a synced tombstone is kept. It cannot be dropped the moment the
# active backend confirms the delete: another device syncing later has to learn
# the record is gone, and its only evidence is the tombstone.
TOMBSTONE_TTL_DAYS = 30


class LocalStorage:
    """The JSON store for one dashboard."""

    def __init__(self, dashboard: str, storage_dir: Path | None = None):
        """Initialize local storage for a dashboard.

        Args:
            dashboard: Dashboard name (e.g., "projects")
            storage_dir: Directory for storage files (default:
                ~/.local/share/projection)
        """
        self.dashboard = dashboard
        self.storage_dir = storage_dir or DEFAULT_STORAGE_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = self.storage_dir / f"{dashboard}.json"
        # Records that failed validation on the last load. Held so `save` can
        # write them back rather than dropping them on the floor.
        self._unreadable: list[Any] = []

    @property
    def path(self) -> Path:
        """Where this store lives."""
        return self._file_path

    # ==================== Raw file I/O ====================

    def _empty(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "items": [],
            "tombstones": [],
            "backends": {},
        }

    def _load_raw(self) -> dict:
        """Read the file, upgrading or quarantining it as needed."""
        if not self._file_path.exists():
            return self._empty()

        try:
            with open(self._file_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Atomic writes mean this should be unreachable; if it happens the
            # file is damaged, and damaged is not the same as empty.
            self._quarantine("unparseable")
            return self._empty()

        if not isinstance(data, dict):
            self._quarantine("unparseable")
            return self._empty()

        version = data.get("schema")
        if version == SCHEMA_VERSION:
            return data
        if isinstance(version, int) and OLDEST_MIGRATABLE <= version < SCHEMA_VERSION:
            migrated = _migrate(data, from_version=version)
            # Keep the original before overwriting it. A migration is the one
            # moment this file is rewritten wholesale into a shape its previous
            # reader cannot understand, so a bug here would otherwise be
            # unrecoverable — and this is the store of record.
            self._archive(f"schema-{version}")
            self._save_raw(migrated)
            return migrated

        # Either older than anything we can read, or newer than this build
        # understands. Both are someone else's data — move it aside, do not
        # overwrite it.
        self._quarantine(f"schema-{version}")
        return self._empty()

    def _aside_path(self, reason: str) -> Path:
        """Where a file goes when it cannot be used as it stands.

        `reason` is sanitized rather than interpolated, because one caller builds
        it from a value **read out of the file itself** (`schema-<version>`). A
        stored `"schema": "../../../../tmp/x"` would otherwise make the rename
        below move the store outside the data directory — the process doing the
        moving is Projection's, so the path must not be the file's to choose.
        """
        safe = _UNSAFE_IN_NAME.sub("_", str(reason))[:40] or "aside"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.storage_dir / f"{self.dashboard}.{safe}.{stamp}.json"
        # Belt and braces: the name is already free of separators, and this holds
        # even if that ever changes.
        if target.parent.resolve() != self.storage_dir.resolve():
            raise ValueError(
                f"refusing to write {target} outside {self.storage_dir}"
            )
        return target

    def _quarantine(self, reason: str) -> Optional[Path]:
        """Move the current file aside, keeping it readable."""
        if not self._file_path.exists():
            return None
        target = self._aside_path(reason)
        try:
            os.replace(self._file_path, target)
        except OSError:
            return None
        return target

    def _archive(self, reason: str) -> Optional[Path]:
        """Copy the current file aside, leaving the original in place."""
        if not self._file_path.exists():
            return None
        target = self._aside_path(reason)
        try:
            shutil.copy2(self._file_path, target)
        except OSError:
            return None
        return target

    def _save_raw(self, data: dict) -> None:
        """Save raw JSON data atomically."""
        fd, tmp = tempfile.mkstemp(dir=self.storage_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._file_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ==================== Projects ====================

    def load(self) -> list[Project]:
        """Load every project."""
        data = self._load_raw()
        projects: list[Project] = []
        seen: set[str] = set()
        unreadable: list[Any] = []

        for item in data.get("items", []):
            try:
                project = Project.model_validate(item)
            except Exception:
                # Kept, not dropped: `save` writes these back verbatim.
                unreadable.append(item)
                continue
            if project.id in seen:
                continue  # defensive de-dupe
            seen.add(project.id)
            projects.append(project)

        self._unreadable = unreadable + list(data.get("unreadable_items", []))
        return projects

    def save(self, projects: list[Project]) -> None:
        """Save projects, preserving tombstones, sync state, and stray records."""
        data = self._load_raw()
        data["schema"] = SCHEMA_VERSION
        data["items"] = [p.model_dump(mode="json") for p in projects]
        data["last_modified"] = now_stamp()
        if self._unreadable:
            data["unreadable_items"] = self._unreadable
        else:
            data.pop("unreadable_items", None)
        self._save_raw(data)

    def get_item(self, key: str) -> Optional[Project]:
        """Get a single project by id."""
        for project in self.load():
            if project.id == key:
                return project
        return None

    def add_item(self, project: Project) -> None:
        """Add a project."""
        projects = self.load()
        projects.append(project)
        self.save(projects)

    def update_item(self, key: str, **updates: Any) -> bool:
        """Update a project's canonical fields, located by id.

        Accepts any of `FIELD_NAMES` plus `dirty`. A field left out — or passed
        as None — is untouched, so a caller can write one column without
        restating the rest. Every field actually written gets its own
        `updated_at` stamp, which is what lets a merge tell two people editing
        different columns apart.

        Returns:
            True if the project was found.
        """
        projects = self.load()
        for project in projects:
            if project.id != key:
                continue
            _apply(project, updates)
            self.save(projects)
            return True
        return False

    def replace_item(self, project: Project) -> bool:
        """Store `project` over the record with the same id."""
        projects = self.load()
        for index, existing in enumerate(projects):
            if existing.id == project.id:
                projects[index] = project
                self.save(projects)
                return True
        return False

    def delete_item(self, key: str) -> bool:
        """Delete a project, leaving a tombstone behind.

        Returns:
            True if the project was found and deleted.
        """
        projects = self.load()
        doomed = next((p for p in projects if p.id == key), None)
        if doomed is None:
            return False

        remaining = [p for p in projects if p.id != key]
        data = self._load_raw()
        data["schema"] = SCHEMA_VERSION
        data["items"] = [p.model_dump(mode="json") for p in remaining]
        data["tombstones"] = [
            t for t in data.get("tombstones", []) if t.get("id") != key
        ] + [
            {
                "id": doomed.id,
                "deleted_at": now_stamp(),
                # Which backend key to delete, for each backend that has one.
                "remote": {
                    name: {"id": link.id}
                    for name, link in doomed.remote.items()
                    if link.id
                },
                "synced": [],
            }
        ]
        data["last_modified"] = now_stamp()
        self._save_raw(data)
        return True

    def set_clean(self, key: str, backend: Optional[str] = None) -> bool:
        """Mark a project as synced: clear `dirty` and snapshot the merge base.

        The base is taken *here* rather than at write time on purpose — what
        actually reached the backend is what the record held when the write
        succeeded, which may already include an edit made mid-flight.
        """
        projects = self.load()
        for project in projects:
            if project.id != key:
                continue
            project.dirty = False
            if backend:
                project.set_base(backend)
            self.save(projects)
            return True
        return False

    def link_remote(
        self,
        key: str,
        backend: str,
        remote_id: Optional[str],
        *,
        modified_at: Optional[str] = None,
    ) -> Optional[Project]:
        """Attach a backend's key to a project.

        Returns the stored project — with whatever field values it holds now,
        which may have changed since the write that earned this id — or None if
        it is gone.
        """
        projects = self.load()
        for project in projects:
            if project.id != key:
                continue
            project.link_remote(backend, remote_id, modified_at=modified_at)
            self.save(projects)
            return project
        return None

    # ==================== Tombstones ====================

    def tombstones(self) -> list[dict]:
        """Deletions not yet known to every backend."""
        return list(self._load_raw().get("tombstones", []))

    def mark_tombstone_synced(self, key: str, backend: str) -> bool:
        """Record that `backend` has applied a deletion."""
        data = self._load_raw()
        stones = data.get("tombstones", [])
        for stone in stones:
            if stone.get("id") != key:
                continue
            synced = list(stone.get("synced") or [])
            if backend not in synced:
                synced.append(backend)
            stone["synced"] = synced
            data["tombstones"] = stones
            self._save_raw(data)
            return True
        return False

    def purge_tombstones(self, *, older_than_days: int = TOMBSTONE_TTL_DAYS) -> int:
        """Drop tombstones past their retention window. Returns how many went."""
        data = self._load_raw()
        stones = data.get("tombstones", [])
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400

        def expired(stone: dict) -> bool:
            when = _parse_stamp(stone.get("deleted_at"))
            return when is not None and when < cutoff

        kept = [s for s in stones if not expired(s)]
        if len(kept) == len(stones):
            return 0
        data["tombstones"] = kept
        self._save_raw(data)
        return len(stones) - len(kept)

    # ==================== Sync state ====================

    def get_last_sync(self, backend: Optional[str] = None) -> Optional[str]:
        """When a backend last synced successfully.

        With no backend named, the most recent across all of them — which is
        what the UI's "synced Xh ago" note means.
        """
        backends = self._load_raw().get("backends", {})
        if not isinstance(backends, dict):
            return None
        if backend is not None:
            entry = backends.get(backend)
            return entry.get("last_sync") if isinstance(entry, dict) else None
        stamps = [
            entry["last_sync"]
            for entry in backends.values()
            if isinstance(entry, dict) and entry.get("last_sync")
        ]
        return max(stamps) if stamps else None

    def set_last_sync(
        self, backend: str, timestamp: Optional[str] = None
    ) -> None:
        """Record a successful sync for `backend`."""
        data = self._load_raw()
        backends = data.get("backends")
        if not isinstance(backends, dict):
            backends = {}
        entry = backends.get(backend)
        if not isinstance(entry, dict):
            entry = {}
        entry["last_sync"] = timestamp or now_stamp()
        backends[backend] = entry
        data["backends"] = backends
        self._save_raw(data)

    def has_local_data(self) -> bool:
        """Whether a usable store exists.

        A migratable older layout counts as present — it is real data, and
        answering "no" here is what used to make startup wait on the network
        before showing anything.
        """
        if not self._file_path.exists():
            return False
        data = self._load_raw()
        return bool(data.get("items"))


# ==================== Helpers ====================


def _apply(project: Project, updates: dict[str, Any]) -> None:
    """Write canonical fields onto a project, stamping what changed."""
    touched: list[str] = []
    for name in FIELD_NAMES:
        if name not in updates:
            continue
        value = updates[name]
        if value is None:
            continue  # "not supplied", not "clear it"
        if name == "assigned":
            value = people(value)
        elif name == "due_date":
            iso = to_iso_date(value)
            if iso is None:
                continue  # unparseable: skip rather than store a typo
            value = iso
        elif name == "starred":
            value = bool(value)
        setattr(project.fields, name, value)
        touched.append(name)
        # Typing a new value into a conflicted field *is* a decision: the user's
        # value wins, and their side's value is acknowledged so the next fetch
        # doesn't raise the same conflict again. Without this, a conflicted field
        # could never be cleared by simply editing it.
        if name in project.conflicts:
            project.resolve_conflict(name, take_theirs=False)

    if touched:
        project.touch(*touched)
    if "dirty" in updates:
        project.dirty = bool(updates["dirty"])


def _parse_stamp(value: Any) -> Optional[float]:
    """An ISO-8601 stamp as a POSIX timestamp, or None if unreadable."""
    parsed = parse_stamp(value)
    return parsed.timestamp() if parsed is not None else None


def _migrate(data: dict, *, from_version: int) -> dict:
    """Upgrade a stored file to the current layout."""
    if from_version == 2:
        return _migrate_v2_to_v3(data)
    raise ValueError(f"no migration from schema {from_version}")


def _migrate_v2_to_v3(data: dict) -> dict:
    """v2 (row-id keyed, one unnamed backend) -> v3.

    Two choices here are load-bearing:

    - **The v2 `local_id` becomes the permanent id.** It is already unique, and
      reusing it keeps identity stable for anything holding one — which for a
      row created but not yet written is the only key it has.
    - **A `dirty` record gets no merge base.** Its fields differ from the
      backend by definition, so snapshotting them as the base would assert
      "local matches remote" and license a later merge to discard the very edit
      that made it dirty. No base means "unknown", which keeps the local copy.

    Field times are set from the file's own `last_sync` for clean records — they
    *are* the synced values — and from `last_modified` for dirty ones, whose
    local edits came after.
    """
    synced_at = data.get("last_sync")
    edited_at = data.get("last_modified") or synced_at or now_stamp()

    items: list[dict] = []
    for old in data.get("items", []):
        if not isinstance(old, dict):
            continue
        props = old.get("properties") or {}
        dirty = bool(old.get("dirty"))

        fields = ProjectFields(
            title=str(old.get("title") or "Untitled"),
            status=props.get("status"),
            assigned=people(props.get("assigned")),
            # v2 stored the display form; ISO is canonical now.
            due_date=to_iso_date(props.get("due_date")) or None,
            note=props.get("update"),
            # v2 carried the Smartsheet cell text rather than a boolean.
            starred=str(props.get("sync") or "").strip().lower() == "yes",
        )

        row_id = old.get("row_id")
        link = RemoteLink(
            id=None if row_id is None else str(row_id),
            base=None if dirty else fields.model_copy(deep=True),
        )

        stamp = edited_at if dirty else (synced_at or edited_at)
        attrs: dict[str, Any] = {
            "fields": fields,
            "updated_at": {name: stamp for name in FIELD_NAMES},
            "remote": {_V2_BACKEND: link} if row_id is not None or dirty else {},
            "dirty": dirty,
        }
        # Only override the minted id when v2 actually carried one; passing
        # id=None would fail validation rather than fall back to the factory.
        carried_id = str(old.get("local_id") or "").strip()
        if carried_id:
            attrs["id"] = carried_id
        items.append(Project(**attrs).model_dump(mode="json"))

    migrated = {
        "schema": SCHEMA_VERSION,
        "items": items,
        "tombstones": [],
        "backends": {},
        "last_modified": now_stamp(),
        # What the file used to record, kept so the upgrade is auditable.
        "migrated_from": {"schema": 2, "last_sync": synced_at},
    }
    if synced_at:
        migrated["backends"] = {_V2_BACKEND: {"last_sync": synced_at}}
    return migrated
