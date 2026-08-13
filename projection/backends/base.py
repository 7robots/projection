"""What a backend is.

A backend is a place Projection can mirror the local store to. It is never the
source of record — `local_storage` is — so a backend's job is narrow: report
what it holds, and apply changes to individual records.

The split that matters between backends is **not** which service they are, it is
whether they can address one record at a time:

| Backend            | Shape                 | Concurrency                          |
|--------------------|-----------------------|--------------------------------------|
| Smartsheet         | row-addressable REST  | per-row PUT, `modifiedAt` per row    |
| Cloudflare D1      | SQL                   | real primary keys, transactions       |
| Google Sheets      | whole range           | no real compare-and-swap              |
| CSV in R2          | whole object          | ETag / `If-Match` gives actual CAS    |

The bottom two cannot honour `update_record` without rewriting the whole
document, so they must declare `row_addressable=False` and serialize their
writes. `Capabilities` exists so callers can ask rather than assume; pretending
all four are alike is how a lost-update bug gets written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from ..columns import CANONICAL as DEFAULT_COLUMNS
from ..models import FIELD_NAMES, Project


class BackendError(Exception):
    """A backend operation failed."""


class StaleRecordError(BackendError):
    """A compare-and-swap write was refused: the record changed underneath.

    Only raised by a backend declaring `supports_cas`. It is not an error in the
    ordinary sense — it is the backend catching a lost update that would
    otherwise have happened silently. The caller's answer is to refetch and let
    the merge decide, never to retry the write.
    """


@dataclass(frozen=True)
class Capabilities:
    """What a backend can actually do.

    Defaults describe the easiest possible backend (a row-addressable store with
    plain text fields), so a new implementation has to opt *out* of a guarantee
    rather than silently lack it.
    """

    # Can one record be written without rewriting the others? False for
    # whole-document targets (a spreadsheet range, a CSV object).
    row_addressable: bool = True
    # Does it offer compare-and-swap (ETag / If-Match / version column)? Without
    # it, two writers can lose each other's changes and nothing detects it.
    supports_cas: bool = False
    # Can it create its own target (sheet, table, object) on first connect?
    can_provision: bool = False
    # Are assignees first-class contacts (needing an email) rather than text?
    typed_contacts: bool = False
    # Does it report a per-record modification time?
    reports_modified_at: bool = False


@dataclass
class ProbeResult:
    """What a backend found when asked to check its target."""

    # True when the target exists and has every field Projection needs.
    ready: bool
    # False means "nothing there yet" — the case `provision()` answers.
    exists: bool
    # Canonical field names the target has no column for.
    missing_fields: tuple[str, ...] = ()
    # Human-readable specifics, shown in setup and in errors.
    detail: str = ""
    # True when the target is reachable but missing structure the backend can
    # create itself — a D1 database with no table in it yet. Setup calls
    # `provision()` for this rather than showing the mapping dialog, which is for
    # the *other* not-ready case: structure that exists under different names.
    # A spreadsheet is never repairable: a sheet without the right columns is a
    # vocabulary question, and answering it by adding columns to someone's sheet
    # is not a decision a setup wizard gets to make.
    repairable: bool = False

    @property
    def needs_provisioning(self) -> bool:
        return not self.exists


@dataclass
class FieldMap:
    """Canonical field name <-> this target's column titles."""

    columns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLUMNS))

    def column(self, field_name: str) -> str:
        """The remote column title for a canonical field."""
        try:
            return self.columns[field_name]
        except KeyError:
            raise BackendError(
                f"No column mapped for the {field_name!r} field. Add it under "
                "the backend's [.columns] table in config.toml."
            )

    def field(self, column: str) -> Optional[str]:
        """The canonical field a remote column title maps to, if any."""
        for name, title in self.columns.items():
            if title == column:
                return name
        return None

    @property
    def titles(self) -> tuple[str, ...]:
        """Every mapped column title, in canonical field order."""
        return tuple(self.columns[name] for name in FIELD_NAMES if name in self.columns)

    def unmapped(self) -> tuple[str, ...]:
        """Canonical fields with no column mapped."""
        return tuple(name for name in FIELD_NAMES if name not in self.columns)


@dataclass
class ProvisionResult:
    """What `provision()` created.

    The target's id comes back because the caller has to *persist* it — a sheet
    created but never written to config.toml is a sheet nobody can find again.
    """

    target_id: str
    name: str = ""
    detail: str = ""


@dataclass
class RecordRef:
    """A record's identity in a backend, as reported by a write."""

    id: str
    modified_at: Optional[str] = None


@runtime_checkable
class Backend(Protocol):
    """The contract `SyncCoordinator` talks to.

    Implementations own their own transport and credentials. They must not
    assume anything about Projection's identity model beyond what they are
    handed: a backend never invents a `Project.id`.

    Three contract rules that exist because breaking them causes data loss:

    - **`fetch()` sets each returned project's `remote[name]` link and merge
      base**, and leaves `Project.id` provisional — the caller replaces it with
      the id of whichever local record already maps to that key.
    - **`fetch()` returns the *complete* set, or raises.** A record the backend
      knows but omits reads as "deleted there", and the merge will drop the local
      copy. So a paginated or filtered read must gather every page before
      returning, and a partial result must raise rather than return what it got.
    - **`update_record()` writes only the fields it is given.** A field absent
      from `changes` must be left alone, not cleared. An explicitly empty value
      is the only thing that means "clear it".
    """

    name: str
    capabilities: Capabilities

    async def ensure_ready(self) -> None:
        """Load credentials and open the transport, raising if that fails.

        Called before anything else, so a locked 1Password or a missing token is
        reported as what it is instead of surfacing later as a generic sync
        failure. It belongs to the backend rather than the caller: the panel used
        to authenticate a Smartsheet client directly, which had nothing to say
        about a backend reading a different credential.
        """
        ...

    async def probe(self) -> ProbeResult:
        """Check the target exists and has the columns Projection needs."""
        ...

    async def provision(self) -> ProvisionResult:
        """Create the target. Only called when `can_provision` is true.

        The returned id is what setup writes to config.toml, and the backend
        must be usable afterwards without being rebuilt.
        """
        ...

    async def target_columns(self) -> tuple[str, ...]:
        """The column titles the target actually has, in its own order.

        For adopting a target whose vocabulary differs from Projection's: the
        mapping dialog offers these, so a user picks from real columns rather
        than typing titles that may not exist. Returns () when the backend has
        no column concept.
        """
        ...

    async def fetch(self) -> list[Project]:
        """Every record the target holds, as canonical projects."""
        ...

    async def create_record(
        self, fields: dict[str, Any], *, project_id: str = ""
    ) -> RecordRef:
        """Add a record and return the key the backend assigned it.

        `project_id` is the project's local id. A backend whose own key space is
        arbitrary (a SQL primary key) can adopt it, which makes the create
        idempotent — an upsert on a known key cannot duplicate a record the way
        an appended spreadsheet row can, and the same project then carries the
        same id on every device. A backend whose keys are assigned for it (a
        Smartsheet row id) ignores it.
        """
        ...

    async def update_record(
        self,
        remote_id: str,
        changes: dict[str, Any],
        *,
        expected_modified_at: Optional[str] = None,
    ) -> None:
        """Write just `changes` onto an existing record.

        `expected_modified_at` is the record's modification time as last seen. A
        backend declaring `supports_cas` must refuse the write and raise
        `StaleRecordError` when the stored value differs — that is the difference
        between detecting a lost update and causing one. A backend without CAS
        ignores it; the three-way merge is the only protection it has.
        """
        ...

    async def delete_record(self, remote_id: str) -> None:
        """Remove a record."""
        ...

    def assignee_options(self) -> list[str]:
        """Names the target will accept as assignees ([] if it doesn't care)."""
        ...

    async def aclose(self) -> None:
        """Release any transport this backend owns."""
        ...
