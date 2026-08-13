"""Field-level three-way merge.

A pure function over three snapshots of one project's fields, deliberately kept
out of `sync.py`: this is where the data-loss decisions live, and they should be
readable and testable without an event loop, a store, or a backend.

**Why three-way.** With only local and remote values you can tell they differ but
not *who changed* — so the only available policies are "remote always wins"
(discards the user's typing) or "local always wins" (discards a colleague's).
The base — what both sides last agreed on — is what makes the question
answerable. `remote[backend].base` carries it.

**Why per field.** Two people editing different columns of the same project is
the common case on a shared sheet, and it is not a conflict. Only the same field
changing on both sides is.

**Both sides changed the same field to different values** is a real conflict, and
it is *not* resolved here. The local value stays on display, the remote value is
recorded, and nothing is pushed for that field until the user chooses — a silent
auto-resolution is the one outcome that loses work without telling anyone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import (
    FIELD_NAMES,
    FieldConflict,
    ProjectFields,
    field_value_out,
    now_stamp,
)


@dataclass
class MergeOutcome:
    """The result of merging one project."""

    # The values to display and store.
    fields: ProjectFields
    # The new merge base. Not simply the remote snapshot: a field the local side
    # changed keeps its old base, which is what keeps it looking "unsynced" on
    # the next pass instead of being quietly forgotten.
    base: ProjectFields
    # Fields both sides changed differently, awaiting the user.
    conflicts: dict[str, FieldConflict] = field(default_factory=dict)
    # Fields where the local value won and still needs pushing.
    to_push: set[str] = field(default_factory=set)
    # Fields where the remote value was adopted.
    took_remote: set[str] = field(default_factory=set)

    @property
    def dirty(self) -> bool:
        """Whether unsynced local changes remain.

        Conflicted fields do not count: nothing is pushed for them until the
        user resolves the conflict, so treating them as dirty would mean
        overwriting the other side's value behind their back.
        """
        return bool(self.to_push)


def merge_fields(
    *,
    base: Optional[ProjectFields],
    mine: ProjectFields,
    theirs: ProjectFields,
    backend: str,
    theirs_modified_at: Optional[str] = None,
    existing: Optional[dict[str, FieldConflict]] = None,
) -> MergeOutcome:
    """Merge one project's fields three ways.

    Args:
        base: what both sides last agreed on, or None if never synced.
        mine: the local values.
        theirs: the backend's values.
        backend: the backend's name, recorded on any conflict raised.
        theirs_modified_at: the backend's modification time, if it reports one.
            Shown to the user when choosing; never used to auto-resolve.
        existing: conflicts already recorded, so a re-fetch doesn't reset when
            they were first noticed.
    """
    if base is None:
        # Never synced, so nothing can be said about who changed what. The local
        # copy wins wholesale and stays unsynced — the same conservative rule the
        # v2 migration relies on for records that were dirty at upgrade time.
        # Raising a conflict on every field here would be noise, not information.
        return MergeOutcome(
            fields=mine.model_copy(deep=True),
            base=mine.model_copy(deep=True),
            to_push=set(_changed_against_nothing(mine, theirs)),
        )

    previous = existing or {}
    merged = mine.model_copy(deep=True)
    new_base = base.model_copy(deep=True)
    conflicts: dict[str, FieldConflict] = {}
    to_push: set[str] = set()
    took_remote: set[str] = set()

    for name in FIELD_NAMES:
        base_value = getattr(base, name)
        my_value = getattr(mine, name)
        their_value = getattr(theirs, name)

        local_changed = my_value != base_value
        remote_changed = their_value != base_value

        if not local_changed and not remote_changed:
            continue  # nobody touched it

        if local_changed and not remote_changed:
            # Keep mine, and keep the old base so it still reads as unsynced.
            setattr(merged, name, my_value)
            to_push.add(name)
            continue

        if remote_changed and not local_changed:
            setattr(merged, name, their_value)
            setattr(new_base, name, their_value)
            took_remote.add(name)
            continue

        # Both sides changed it.
        if my_value == their_value:
            # Converged independently — no conflict, and now agreed.
            setattr(merged, name, their_value)
            setattr(new_base, name, their_value)
            took_remote.add(name)
            continue

        # A genuine conflict. Show mine, remember theirs, push nothing, and
        # leave the base alone so the conflict survives until it is resolved.
        setattr(merged, name, my_value)
        was = previous.get(name)
        conflicts[name] = FieldConflict(
            backend=backend,
            mine=field_value_out(my_value),
            theirs=field_value_out(their_value),
            base=field_value_out(base_value),
            detected_at=was.detected_at if was else now_stamp(),
            theirs_modified_at=theirs_modified_at,
        )

    return MergeOutcome(
        fields=merged,
        base=new_base,
        conflicts=conflicts,
        to_push=to_push,
        took_remote=took_remote,
    )


def _changed_against_nothing(
    mine: ProjectFields, theirs: ProjectFields
) -> list[str]:
    """Fields to push when there is no base: whatever the backend lacks."""
    return [
        name
        for name in FIELD_NAMES
        if getattr(mine, name) != getattr(theirs, name)
    ]
