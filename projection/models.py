"""The canonical project model.

Projection's local JSON store is the **source of record**, not a cache. Two
consequences shape everything here:

- **Identity is local and permanent.** `Project.id` is minted here and never
  changes. A backend's own key — a Smartsheet row id, a D1 primary key — is
  *mapped* alongside it in `remote`, so adopting, switching, or adding a backend
  is a mapping change rather than a data migration, and no backend gets to
  decide what a project *is*.
- **Fields carry their own modification times.** `updated_at` is per field, not
  per record, so two people editing different columns of the same project do not
  conflict. `remote[backend].base` holds the last-synced snapshot, which is what
  makes "who changed this?" answerable at all: without a base, a merge can only
  ever pick a side.

Dates are stored ISO (`YYYY-MM-DD`) and rendered `M/D/YYYY`. Storing the display
form — as the old cache did — makes every backend re-parse a locale-shaped string.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from .statuses import status_rank

# Every canonical field, in display order. Anything iterating fields (merges,
# migrations, per-field timestamps) uses this rather than its own list.
FIELD_NAMES: tuple[str, ...] = (
    "title",
    "status",
    "assigned",
    "due_date",
    "note",
    "starred",
)


def new_id() -> str:
    """Mint a permanent local project id."""
    return uuid.uuid4().hex


def now_stamp() -> str:
    """A UTC ISO-8601 timestamp for field modification times.

    Timezone-aware and second-resolution: these are compared against remote
    timestamps from different machines, and a naive one cannot be.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_stamp(value: Any) -> Optional[datetime]:
    """An ISO-8601 stamp as a timezone-**aware** datetime, or None if unreadable.

    This exists because the codebase legitimately holds both kinds, and
    subtracting a naive datetime from an aware one raises `TypeError`. Every
    comparison of stamps must go through here.

    **Naive input is read as local time**, not UTC. Every naive stamp in the wild
    was written by the v2 store's `datetime.now()`, which is local — so calling
    it UTC would report an age off by the UTC offset (four hours, in the timezone
    this was written in), and "synced just now" would read as "synced 4h ago".
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()  # attaches the local zone
    return parsed


def to_display_date(iso: Optional[str]) -> str:
    """Stored ISO date -> the M/D/YYYY shown in the UI."""
    if not iso:
        return ""
    try:
        parsed = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
    except ValueError:
        return str(iso)
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def to_iso_date(display: Optional[str]) -> Optional[str]:
    """A typed or stored date -> the ISO form used everywhere internally.

    Returns "" for empty input (which clears the value) and None when the text
    cannot be parsed, so a typo is skipped rather than stored.
    """
    text = (display or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


class Person(BaseModel):
    """An assignee.

    The email is carried locally, not just the display name. A contact-typed
    remote column (Smartsheet's MULTI_CONTACT) needs an address to write, and
    resolving one from column metadata at write time is why an unrecognised name
    used to make the whole write fail. A plain-text backend simply ignores it.
    """

    name: str
    email: str = ""


class ProjectFields(BaseModel):
    """The canonical column set every backend maps to.

    Names here are Projection's vocabulary; a backend maps them onto its own
    column titles. `note` is what the Smartsheet calls "Update", and `starred`
    is a real boolean rather than that sheet's "Yes"/"No" text.
    """

    title: str = ""
    status: Optional[str] = None
    assigned: list[Person] = Field(default_factory=list)
    due_date: Optional[str] = None  # ISO YYYY-MM-DD
    note: Optional[str] = None
    starred: bool = False


def field_value_out(value: Any) -> Any:
    """A field value in JSON-safe form, for storing inside a conflict record."""
    if isinstance(value, list):
        return [
            item.model_dump() if isinstance(item, Person) else item for item in value
        ]
    return value


def field_value_in(name: str, value: Any) -> Any:
    """A stored conflict value back in the shape `ProjectFields` expects."""
    if name == "assigned":
        return people(value)
    return value


def field_value_text(name: str, value: Any) -> str:
    """A field value as one line of display text, for the conflict chooser."""
    if name == "assigned":
        names = [
            item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
            for item in (value or [])
        ]
        return ", ".join(n for n in names if n) or "(nobody)"
    if name == "starred":
        return "starred" if value else "not starred"
    if name == "due_date":
        return to_display_date(value) or "(no date)"
    text = "" if value is None else str(value)
    return text or "(empty)"


class FieldConflict(BaseModel):
    """One field that both sides changed, differently, since the last sync.

    Deliberately *not* auto-resolved. Values are stored JSON-safe rather than as
    live field objects, so a conflict survives a restart in the store.
    """

    backend: str
    mine: Any = None
    theirs: Any = None
    # What both sides last agreed on — the reason we know this is a conflict at
    # all, and useful context when choosing.
    base: Any = None
    detected_at: str = ""
    # The backend's own modification time, when it reports one. Shown to the
    # user; never used to pick a winner automatically.
    theirs_modified_at: Optional[str] = None


class RemoteLink(BaseModel):
    """What one backend knows about a project."""

    # The backend's own key, held as text so an int row id and a string primary
    # key are the same shape here.
    id: Optional[str] = None
    # The backend's own modification time, when it reports one.
    modified_at: Optional[str] = None
    # The field values as of the last successful sync — the base of a three-way
    # merge. None means "never synced", which is not the same as "unchanged".
    base: Optional[ProjectFields] = None


class Project(BaseModel):
    """A project, keyed by its permanent local id."""

    id: str = Field(default_factory=new_id)
    fields: ProjectFields = Field(default_factory=ProjectFields)
    # Field name -> ISO-8601 UTC time it last changed locally.
    updated_at: dict[str, str] = Field(default_factory=dict)
    # Backend name -> what that backend knows.
    remote: dict[str, RemoteLink] = Field(default_factory=dict)
    # Canonical field name -> an unresolved conflict on it. Persisted, so a
    # conflict waits for the user rather than expiring when the app closes.
    conflicts: dict[str, FieldConflict] = Field(default_factory=dict)
    # Set while local edits have not reached the backend. Persisted, so a write
    # that failed (or was cut short by quitting) survives the next refresh
    # instead of being silently overwritten by the remote snapshot.
    dirty: bool = False

    # -- identity -----------------------------------------------------------

    @property
    def key(self) -> str:
        """The project's identity. Always the local id."""
        return self.id

    def remote_id(self, backend: str) -> Optional[str]:
        """This project's key in `backend`, or None if it isn't there yet."""
        link = self.remote.get(backend)
        return link.id if link else None

    def link_remote(
        self,
        backend: str,
        remote_id: Optional[str],
        *,
        modified_at: Optional[str] = None,
    ) -> None:
        """Record (or update) this project's key in `backend`."""
        link = self.remote.get(backend) or RemoteLink()
        link.id = None if remote_id is None else str(remote_id)
        if modified_at is not None:
            link.modified_at = modified_at
        self.remote[backend] = link

    def set_base(self, backend: str) -> None:
        """Snapshot the current fields as `backend`'s merge base."""
        link = self.remote.get(backend) or RemoteLink()
        link.base = self.fields.model_copy(deep=True)
        self.remote[backend] = link

    def base_for(self, backend: str) -> Optional[ProjectFields]:
        """`backend`'s merge base, or None if this project never synced to it."""
        link = self.remote.get(backend)
        return link.base if link else None

    # -- edits --------------------------------------------------------------

    def touch(self, *names: str, stamp: Optional[str] = None) -> None:
        """Mark fields as changed now (or at `stamp`)."""
        when = stamp or now_stamp()
        for name in names:
            self.updated_at[name] = when

    def changed_at(self, name: str) -> Optional[str]:
        """When `name` last changed locally, if it is known."""
        return self.updated_at.get(name)

    # -- conflicts ----------------------------------------------------------

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def conflict_fields(self) -> list[str]:
        """Conflicted fields, in canonical order."""
        return [name for name in FIELD_NAMES if name in self.conflicts]

    def resolve_conflict(self, name: str, *, take_theirs: bool) -> bool:
        """Settle one conflicted field. Returns True if there was one.

        Either way the base moves to *their* value, because either way we have
        now seen it. That is what makes the resolution stick:

        - take theirs -> value and base both become theirs, so the field reads as
          agreed and nothing is pushed.
        - keep mine   -> base becomes theirs while the value stays mine, so the
          field reads as a plain local change and gets pushed on the next write.

        Leaving the base alone instead would have the next fetch raise the very
        same conflict again.
        """
        conflict = self.conflicts.pop(name, None)
        if conflict is None:
            return False

        their_value = field_value_in(name, conflict.theirs)
        link = self.remote.get(conflict.backend)
        if link is not None and link.base is not None:
            setattr(link.base, name, their_value)

        if take_theirs:
            setattr(self.fields, name, their_value)
        else:
            self.dirty = True
            self.touch(name)
        return True

    # -- display ------------------------------------------------------------

    @property
    def title(self) -> str:
        return self.fields.title

    @property
    def status(self) -> str:
        """The status, defaulting to 'Not started'."""
        return self.fields.status or "Not started"

    @property
    def assigned_names(self) -> list[str]:
        return [p.name for p in self.fields.assigned if p.name]

    @property
    def assigned_str(self) -> str:
        """Assignees as a comma-separated string."""
        return ", ".join(self.assigned_names)

    @property
    def due_date(self) -> str:
        """The due date in display format (M/D/YYYY)."""
        return to_display_date(self.fields.due_date)

    @property
    def due_date_iso(self) -> str:
        """The due date as stored (ISO), or ""."""
        return self.fields.due_date or ""

    @property
    def note_text(self) -> str:
        """The latest-update text."""
        return self.fields.note or ""

    @property
    def is_starred(self) -> bool:
        """Whether this project is starred.

        Starring is Projection's own generic "pin this" flag. The exec-summary
        script reads it as "include me"; nothing in the core knows about that.
        """
        return self.fields.starred

    def matches(self, text: str) -> bool:
        """Case-insensitive filter across title, note, assignees, and status."""
        needle = text.lower()
        return (
            needle in self.title.lower()
            or needle in self.note_text.lower()
            or needle in self.assigned_str.lower()
            or needle in self.status.lower()
        )


def people(names: Optional[list]) -> list[Person]:
    """Coerce names, dicts, or `Person`s into a `Person` list.

    Callers hand over whatever they have — typed names from the edit modal,
    contact objects from a backend, dicts from stored JSON — and this is the one
    place that normalizes them.
    """
    if not names:
        return []
    out: list[Person] = []
    for entry in names:
        if entry is None:
            continue  # `str(None)` would become an assignee called "None"
        if isinstance(entry, Person):
            out.append(entry.model_copy())
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("email") or "").strip()
            if name:
                out.append(Person(name=name, email=str(entry.get("email") or "")))
        else:
            name = str(entry).strip()
            if name:
                out.append(Person(name=name))
    return out


def sort_projects(projects: list[Project]) -> list[Project]:
    """Display order: starred first, then by status, then title.

    Status order is In progress, Not started, Blocked, On Hold, then Done.
    """
    return sorted(
        projects,
        key=lambda p: (
            0 if p.is_starred else 1,
            status_rank(p.status),
            p.title.lower(),
        ),
    )
