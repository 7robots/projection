"""Sync coordinator between the local store of record and one backend.

Reads and writes hit the local store immediately; the backend is reconciled in
the background. **A backend is optional** — with none configured the store is
simply the whole application, which is the point of it being the source of
record.

Values are reconciled by `merge.merge_fields`, three ways, per field. This module
owns the surrounding bookkeeping: which records are guarded because a write is in
flight, which deletions are tombstoned, and what gets pushed afterwards.
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .backends import Backend, StaleRecordError
from .local_storage import LocalStorage
from .merge import merge_fields
from .models import (
    FIELD_NAMES,
    Project,
    ProjectFields,
    people,
    to_iso_date,
)

# The backend name recorded against records when there is no backend at all.
# Nothing is written under it; it exists so bookkeeping code has a key.
NO_BACKEND = ""


class SyncStatus(Enum):
    """Status of sync operations."""
    IDLE = "idle"
    SYNCING = "syncing"
    ERROR = "error"


@dataclass
class SyncEvent:
    """Event emitted during sync operations."""
    event_type: str  # "sync_started", "sync_complete", "sync_error", "data_updated"
    message: str = ""
    data: Any = None


@dataclass
class AdoptionReport:
    """What the first reconciliation with a newly attached backend did."""

    # Records only the backend had, now in the local store.
    pulled: int = 0
    # Local records matched to an existing backend record by title.
    linked: int = 0
    # Local records created in the backend.
    pushed: int = 0
    # Local records the backend refused. They keep their local values and stay
    # dirty, so nothing is lost — but they are not in the backend.
    failed: int = 0

    @property
    def summary(self) -> str:
        """One line for a notification."""
        parts = []
        if self.pulled:
            parts.append(f"{self.pulled} pulled in")
        if self.linked:
            parts.append(f"{self.linked} matched by title")
        if self.pushed:
            parts.append(f"{self.pushed} pushed up")
        if self.failed:
            parts.append(f"{self.failed} could not be pushed")
        return ", ".join(parts) or "nothing to reconcile"


def _changes(
    title=None, status=None, assigned=None, due_date=None, note=None, starred=None
) -> dict[str, Any]:
    """Collect the fields a caller actually supplied.

    `None` means "not supplied". `starred=False` and `assigned=[]` are real
    values and must survive — which is why this tests against None rather than
    truthiness.
    """
    supplied = {
        "title": title,
        "status": status,
        "assigned": assigned,
        "due_date": due_date,
        "note": note,
        "starred": starred,
    }
    return {name: value for name, value in supplied.items() if value is not None}


class SyncCoordinator:
    """Coordinates the local store with at most one backend.

    Provides:
    - Instant local reads/writes
    - Background sync to the backend, when there is one
    - Periodic polling for remote changes
    - Event callbacks for UI updates

    To stop a background poll clobbering a write that hasn't landed yet, project
    ids with in-flight writes are held in `_pending`; the merge trusts the local
    copy for those, because what actually reached the backend is unknown until
    the write returns.
    """

    def __init__(
        self,
        on_event: Optional[Callable[[SyncEvent], None]] = None,
        poll_interval: float = 30.0,
        *,
        remote: Optional[Backend] = None,
        data_dir: Optional[Path] = None,
    ):
        """Initialize sync coordinator.

        Args:
            on_event: Callback for sync events (for UI updates)
            poll_interval: Seconds between remote polls (0 to disable)
            remote: The backend to sync with, or None for local-only. It carries
                its own name and target, so there is nothing to default here.
            data_dir: Where the local store lives (None = the default).
        """
        self._local = LocalStorage("projects", storage_dir=data_dir)
        self._remote = remote
        self._backend = remote.name if remote is not None else NO_BACKEND
        self._on_event = on_event
        self._poll_interval = poll_interval
        self._poll_task: Optional[asyncio.Task] = None
        self._status = SyncStatus.IDLE
        self._sync_lock = asyncio.Lock()
        self._ready = False  # True after initial sync complete
        self._last_error: Optional[str] = None
        # Project ids with in-flight remote writes, counted: a create and an
        # edit of the same new project overlap, and the first to finish must not
        # unguard it while the second is still running. A key whose write FAILED
        # is deliberately never released, so the local copy keeps winning merges
        # instead of being overwritten by the remote snapshot.
        self._pending: dict[str, int] = {}
        # Strong references to background write tasks. asyncio only holds weak
        # references, so without this a task can be garbage-collected mid-write
        # — losing the write and leaking its pending guard forever.
        self._tasks: set[asyncio.Task] = set()

    @property
    def has_backend(self) -> bool:
        """Whether anything is being synced to at all."""
        return self._remote is not None

    @property
    def busy(self) -> bool:
        """Whether any write is in flight, or failed and still guarded.

        Setup asks this before replacing the coordinator: a write that lands
        after the backend was swapped out would go to the old target, and its
        bookkeeping would be applied to a store this coordinator no longer owns.
        """
        return bool(self._pending) or bool(self._tasks)

    @property
    def backend_name(self) -> str:
        return self._backend

    def _spawn(self, coro) -> asyncio.Task:
        """Schedule a background write, keeping a strong reference to it."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _hold(self, *keys: str) -> None:
        """Guard keys against being overwritten by a concurrent poll."""
        for key in keys:
            self._pending[key] = self._pending.get(key, 0) + 1

    def _release(self, *keys: str) -> None:
        """Drop a guard placed by `_hold`."""
        for key in keys:
            remaining = self._pending.get(key, 0) - 1
            if remaining > 0:
                self._pending[key] = remaining
            else:
                self._pending.pop(key, None)

    def _emit(self, event_type: str, message: str = "", data: Any = None) -> None:
        """Emit a sync event (only after initial sync is complete)."""
        if self._on_event and self._ready:
            self._on_event(
                SyncEvent(event_type=event_type, message=message, data=data)
            )

    @property
    def status(self) -> SyncStatus:
        """Current sync status."""
        return self._status

    @property
    def last_error(self) -> Optional[str]:
        """Message from the last failed fetch, or None if the last one worked.

        Set even during the initial sync, when events aren't emitted yet — so
        a startup failure can't present itself as "you have no projects".
        """
        return self._last_error

    # ==================== Read Operations ====================

    def load(self) -> list[Project]:
        """Load projects from the local store (instant)."""
        return self._local.load()

    def get_item(self, key: str) -> Optional[Project]:
        """Get a single project from the local store by id (instant)."""
        return self._local.get_item(key)

    def last_sync(self) -> Optional[str]:
        """ISO timestamp of the last successful remote fetch, if any."""
        if not self.has_backend:
            return None
        return self._local.get_last_sync(self._backend)

    def assignee_options(self) -> list[str]:
        """Names the backend will accept as assignees ([] with no backend)."""
        if self._remote is None:
            return []
        return self._remote.assignee_options()

    def conflicted(self) -> list[Project]:
        """Projects with at least one field awaiting the user's decision."""
        return [p for p in self._local.load() if p.has_conflicts]

    # ==================== Write Operations ====================

    async def add_item(
        self,
        title: str,
        status: Optional[str] = None,
        assigned: Optional[list] = None,
        due_date: Optional[str] = None,
        note: Optional[str] = None,
        starred: Optional[bool] = None,
    ) -> Project:
        """Add a project (instant local, background remote sync)."""
        project = Project(
            fields=ProjectFields(
                title=title,
                status=status,
                assigned=people(assigned or []),
                due_date=to_iso_date(due_date) or None,
                note=note,
                starred=bool(starred),
            )
        )
        # Everything about a new project changed just now.
        project.touch(*FIELD_NAMES)
        # Nothing to be out of step with when there is no backend.
        project.dirty = self.has_backend
        self._local.add_item(project)
        self._emit("data_updated", f"Added: {title}")

        if self.has_backend:
            self._hold(project.key)
            self._spawn(self._sync_add_to_remote(project))
        return project

    async def update_item(
        self,
        key: str,
        title: Optional[str] = None,
        status: Optional[str] = None,
        assigned: Optional[list] = None,
        due_date: Optional[str] = None,
        note: Optional[str] = None,
        starred: Optional[bool] = None,
    ) -> bool:
        """Update a project (instant local, background remote sync)."""
        changes = _changes(title, status, assigned, due_date, note, starred)
        success = self._local.update_item(
            key, **changes, dirty=self.has_backend
        )

        if success:
            self._emit("data_updated", "Updated item")
            if self.has_backend and changes:
                self._hold(key)
                self._spawn(self._sync_update_to_remote(key, changes))

        return success

    async def delete_item(self, key: str) -> bool:
        """Delete a project (instant local, background remote sync).

        The local delete leaves a tombstone, so the deletion survives a restart
        and cannot be undone by the next refresh.
        """
        project = self._local.get_item(key)
        remote_id = project.remote_id(self._backend) if project else None
        success = self._local.delete_item(key)

        if success:
            self._emit("data_updated", "Deleted item")
            if self.has_backend:
                self._hold(key)
                self._spawn(self._sync_delete_to_remote(key, remote_id))

        return success

    async def toggle_starred(self, key: str, starred: bool) -> bool:
        """Star or unstar a project (instant local, background remote sync)."""
        return await self.update_item(key, starred=starred)

    async def resolve_conflict(
        self, key: str, field_name: str, *, take_theirs: bool
    ) -> bool:
        """Settle one conflicted field, pushing the local value if it won."""
        project = self._local.get_item(key)
        if project is None or field_name not in project.conflicts:
            return False

        project.resolve_conflict(field_name, take_theirs=take_theirs)
        self._local.replace_item(project)
        self._emit("data_updated", "Conflict resolved")

        if not take_theirs and self.has_backend:
            changes = {field_name: getattr(project.fields, field_name)}
            self._hold(key)
            self._spawn(self._sync_update_to_remote(key, changes))
        return True

    # ==================== Sync Operations ====================

    async def initial_sync(self) -> list[Project]:
        """Perform initial sync on startup.

        Returns local data immediately if present (syncing in background),
        otherwise fetches from remote first.
        """
        if not self.has_backend:
            self._ready = True
            return self._local.load()

        if self._local.has_local_data():
            projects = self._local.load()
            self._ready = True
            asyncio.create_task(self._fetch_from_remote())
            return projects

        projects = await self._fetch_from_remote()
        self._ready = True
        return projects

    async def refresh(self) -> list[Project]:
        """Force refresh from remote."""
        if not self.has_backend:
            return self._local.load()
        return await self._fetch_from_remote()

    # ==================== Adoption ====================

    async def adopt(self) -> AdoptionReport:
        """Reconcile the local store with a backend attached just now.

        Ordinary syncing assumes both sides have met: a record missing from the
        backend was deleted there, and a local record carries a link and a merge
        base. The *first* sync after a backend is attached breaks both
        assumptions — every existing record is unknown to the backend, and
        nothing in the ordinary write paths ever pushes an already-saved record.
        Without this step a local-only install that connects a backend would show
        "synced" while none of its work had gone anywhere.

        Three things happen, in order:

        1. **Titles are matched.** A local record and a backend record with the
           same title are taken to be the same project. It is the only handle
           available, it is applied *once*, here, and never during ordinary
           syncing. A title repeated on either side is left unmatched rather
           than guessed at.
        2. **The merge runs**, which brings in whatever only the backend had.
        3. **Everything the backend lacks is pushed**, with local values winning
           for the records step 1 matched — you attach a backend to publish the
           work you already have, not to have it overwritten on arrival.

        Raises whatever the fetch raised: adopting against an unreachable
        backend must not look like an empty one, which would push every local
        record as new the moment it came back.
        """
        if self._remote is None:
            return AdoptionReport()

        report = AdoptionReport()
        async with self._sync_lock:
            remote = await self._remote.fetch()
            local = self._local.load()
            known = {p.id for p in local}

            report.linked = _link_by_title(local, remote, backend=self._backend)
            if report.linked:
                self._local.save(local)
                local = self._local.load()

            merged = self._merge_remote(remote, local)
            report.pulled = sum(1 for p in merged if p.id not in known)
            # A record the backend has never held *is* unsynced, and nothing
            # else says so: `dirty` was rightly false while there was no backend
            # to be out of step with, and the merge leaves such records
            # untouched. Marking them here is what makes a push that fails leave
            # an honest flag instead of a record that reads as in step.
            for project in merged:
                if project.remote_id(self._backend) is None:
                    project.dirty = True
            self._local.save(merged)
            self._local.set_last_sync(self._backend)

        # Outside the lock: each push takes it again for itself.
        for project in merged:
            if project.remote_id(self._backend) is None:
                self._hold(project.key)
                await self._sync_add_to_remote(project)
            elif project.dirty:
                self._hold(project.key)
                await self._sync_update_to_remote(
                    project.key, _fields_dict(project.fields)
                )
            else:
                continue
            # Re-read rather than trust the call: those paths report failure by
            # leaving the record dirty and guarded, not by raising.
            stored = self._local.get_item(project.key)
            if stored is not None and stored.remote_id(self._backend) and not stored.dirty:
                report.pushed += 1
            else:
                report.failed += 1

        self._ready = True
        return report

    def _tombstoned(self) -> tuple[set[str], set[str]]:
        """Ids and backend keys of projects deleted locally but maybe not there.

        A tombstone is what stops the next refresh resurrecting a row the user
        already deleted — durably, across restarts, which an in-memory guard
        could not do.
        """
        ids: set[str] = set()
        remote_ids: set[str] = set()
        for stone in self._local.tombstones():
            if stone.get("id"):
                ids.add(str(stone["id"]))
            link = (stone.get("remote") or {}).get(self._backend) or {}
            if link.get("id"):
                remote_ids.add(str(link["id"]))
        return ids, remote_ids

    def _merge_remote(
        self, remote: list[Project], local: list[Project]
    ) -> list[Project]:
        """Merge a remote snapshot into the local store.

        Matching is by the backend's key, and **the local record's identity
        always survives** — a record from the backend arrives with a provisional
        local id, which is discarded in favour of the id of whatever local record
        already maps to that key. Minting a new one each fetch would dangle every
        key held by queued work.

        Values are reconciled field by field against the merge base (see
        `merge.py`). Three cases bypass that:

        - **tombstoned** — the deletion outranks the snapshot;
        - **guarded by an in-flight write** — what landed is not yet known, so
          the local copy is kept whole;
        - **no local record** — a project we have not seen before.
        """
        pending = self._pending
        dead_ids, dead_remote_ids = self._tombstoned()
        local_by_remote: dict[str, Project] = {}
        for project in local:
            remote_id = project.remote_id(self._backend)
            if remote_id:
                local_by_remote[remote_id] = project

        merged: list[Project] = []
        handled: set[str] = set()

        for incoming in remote:
            remote_id = incoming.remote_id(self._backend)
            if remote_id and remote_id in dead_remote_ids:
                continue  # deleted here; the tombstone outranks the snapshot

            existing = local_by_remote.get(remote_id) if remote_id else None
            if existing is None:
                merged.append(incoming)  # a project we haven't seen before
                continue

            handled.add(existing.id)
            if existing.id in pending:
                merged.append(existing)  # in flight: keep the local copy whole
                continue

            link = incoming.remote.get(self._backend)
            outcome = merge_fields(
                base=existing.base_for(self._backend),
                mine=existing.fields,
                theirs=incoming.fields,
                backend=self._backend,
                theirs_modified_at=link.modified_at if link else None,
                existing=existing.conflicts,
            )

            adopted = existing.model_copy(deep=True)
            adopted.fields = outcome.fields
            adopted.conflicts = outcome.conflicts
            adopted.dirty = outcome.dirty
            adopted.link_remote(
                self._backend,
                remote_id,
                modified_at=link.modified_at if link else None,
            )
            adopted.remote[self._backend].base = outcome.base
            merged.append(adopted)

            if outcome.conflicts:
                self._emit(
                    "conflict",
                    f"{len(outcome.conflicts)} conflicting field(s) on "
                    f"{adopted.title!r} — press c to resolve",
                    adopted,
                )

        # Local-only projects. Dropping any of these would discard typed work.
        # Three ways a record can be absent from the snapshot without having
        # been deleted there:
        #
        #   - a create still in flight (guarded),
        #   - a create that failed (dirty),
        #   - a record the backend has never been told about at all — which is
        #     every record in the store the moment a backend is attached to an
        #     existing local-only install.
        #
        # That last one is why the link is checked rather than just the flags:
        # "missing from the snapshot" only means "deleted there" for a record
        # that was ever *in* there.
        for project in local:
            if project.id in handled or project.id in dead_ids:
                continue
            if (
                project.id in pending
                or project.dirty
                or project.remote_id(self._backend) is None
            ):
                merged.append(project)
                handled.add(project.id)

        return merged

    async def _fetch_from_remote(self) -> list[Project]:
        """Fetch latest data from the backend and update the local store."""
        if self._remote is None:
            return self._local.load()

        self._status = SyncStatus.SYNCING
        self._emit("sync_started", "Syncing...")

        try:
            async with self._sync_lock:
                remote = await self._remote.fetch()
                merged = self._merge_remote(remote, self._local.load())
                self._local.save(merged)
                self._local.set_last_sync(self._backend)
                # Only once a fetch has succeeded: a tombstone dropped while the
                # backend was unreachable would let the row come back.
                self._local.purge_tombstones()
            self._status = SyncStatus.IDLE
            self._last_error = None
            self._emit("sync_complete", f"Synced {len(merged)} items", merged)
            return merged
        except Exception as e:
            self._status = SyncStatus.ERROR
            # Recorded outside the event system so a failure during the initial
            # sync (when events aren't emitted yet) is still visible to the app.
            self._last_error = f"Sync failed: {e}"
            self._emit("sync_error", self._last_error)
            return self._local.load()

    async def _sync_add_to_remote(self, project: Project) -> None:
        """Background task to create the record and store its backend key."""
        assert self._remote is not None
        async with self._sync_lock:
            succeeded = False
            try:
                ref = await self._remote.create_record(
                    _fields_dict(project.fields), project_id=project.id
                )
                if not ref.id:
                    raise RuntimeError(
                        "The backend did not return a key for the new project"
                    )

                # Record the key without discarding fields the user may have
                # changed since the create started.
                current = self._local.link_remote(
                    project.id, self._backend, ref.id, modified_at=ref.modified_at
                )
                if current is None:
                    # Deleted locally while the create was in flight. The record
                    # exists in the backend now and nothing references it, so the
                    # tombstone is the truth — honour it rather than leaving an
                    # orphan to reappear next refresh.
                    await self._remote.delete_record(ref.id)
                    self._local.mark_tombstone_synced(project.id, self._backend)
                    succeeded = True
                    return

                self._emit("data_updated", f"Synced: {project.fields.title}")

                # If it did change mid-flight, push the newer values.
                if current.fields != project.fields:
                    await self._remote.update_record(
                        ref.id, _fields_dict(current.fields)
                    )
                self._local.set_clean(project.id, self._backend)
                succeeded = True
            except Exception as e:
                self._emit("sync_error", f"Failed to sync new item: {e}")
            finally:
                # A failed write keeps its guard, so the unsynced local copy
                # keeps winning merges until it is retried or the app restarts
                # (where the persisted dirty flag takes over).
                if succeeded:
                    self._release(project.key)

    async def _sync_update_to_remote(
        self, key: str, changes: dict[str, Any]
    ) -> None:
        """Background task to push `changes` for one project."""
        assert self._remote is not None
        stale = False
        async with self._sync_lock:
            succeeded = False
            try:
                project = self._local.get_item(key)
                remote_id = project.remote_id(self._backend) if project else None
                if remote_id is None:
                    # The project isn't in the backend yet. Its create task
                    # holds the lock until it lands, then re-pushes whatever the
                    # local record says — which already includes this edit. The
                    # record stays dirty and guarded until that happens.
                    self._emit("sync_started", "Waiting for the new record…")
                    return
                await self._remote.update_record(
                    remote_id,
                    changes,
                    # Only where the backend can act on it. The value is what we
                    # last saw, so the backend can tell "nobody else touched this"
                    # from "someone did" — which no amount of local bookkeeping
                    # can work out on its own.
                    expected_modified_at=self._expected_version(project),
                )
                self._local.set_clean(key, self._backend)
                self._emit("sync_complete", "Changes synced")
                succeeded = True
            except StaleRecordError as e:
                # Not a failure: the backend caught a lost update before it
                # happened. The write is *never* retried — a refetch runs the
                # merge, which is the only thing entitled to decide between two
                # changed values.
                self._emit("sync_error", str(e))
                stale = True
            except Exception as e:
                self._emit("sync_error", f"Failed to sync update: {e}")
            finally:
                if succeeded:
                    self._release(key)

        # Outside the lock: `_fetch_from_remote` takes it too. The record keeps
        # its guard and its dirty flag, so the merge sees the local value as the
        # unsynced side rather than something to overwrite.
        if stale:
            await self._fetch_from_remote()

    def _expected_version(self, project: Optional[Project]) -> Optional[str]:
        """The record's last-seen backend timestamp, for a compare-and-swap.

        None whenever the backend cannot use it, or we have never seen a
        timestamp — passing one it does not honour would suggest a guarantee that
        is not there.
        """
        if project is None or self._remote is None:
            return None
        if not self._remote.capabilities.supports_cas:
            return None
        link = project.remote.get(self._backend)
        return link.modified_at if link else None

    async def _sync_delete_to_remote(
        self, key: str, remote_id: Optional[str]
    ) -> None:
        """Background task to sync a deletion to the backend."""
        assert self._remote is not None
        async with self._sync_lock:
            try:
                if remote_id is None:
                    # Created only locally so far. If a create is still in
                    # flight it will find the record gone and delete the one it
                    # just made; nothing to do here.
                    self._local.mark_tombstone_synced(key, self._backend)
                    self._release(key)
                    return
                await self._remote.delete_record(remote_id)
                self._local.mark_tombstone_synced(key, self._backend)
                self._emit("sync_complete", "Deletion synced")
                self._release(key)
            except Exception as e:
                # Keep the guard. The tombstone means the next refresh won't
                # resurrect the record even so — but the guard is what stops a
                # concurrent write path racing the retry.
                self._emit("sync_error", f"Failed to sync deletion: {e}")

    # ==================== Polling ====================

    def start_polling(self) -> None:
        """Start periodic polling for remote changes."""
        if self.has_backend and self._poll_interval > 0 and self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    def stop_polling(self) -> None:
        """Stop periodic polling."""
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        """Background polling loop."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._fetch_from_remote()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._emit("sync_error", f"Poll error: {e}")


def _link_by_title(
    local: list[Project], remote: list[Project], *, backend: str
) -> int:
    """Link unlinked local records to backend records of the same title.

    Returns how many were linked. Only unambiguous titles are used: one on each
    side, matched case-insensitively after trimming. Nothing here sets a merge
    base — the values still have to be reconciled, and claiming they already
    agree is how a colleague's row gets silently overwritten.
    """

    def key(title: str) -> str:
        return " ".join(title.split()).casefold()

    def unique(projects: list[Project]) -> dict[str, Project]:
        found: dict[str, Project] = {}
        repeated: set[str] = set()
        for project in projects:
            name = key(project.title)
            if not name:
                continue
            if name in found:
                repeated.add(name)
            found[name] = project
        return {name: p for name, p in found.items() if name not in repeated}

    already_taken = {
        p.remote_id(backend) for p in local if p.remote_id(backend)
    }
    candidates = unique(remote)
    linked = 0
    for name, project in unique(local).items():
        if project.remote_id(backend) is not None:
            continue
        match = candidates.get(name)
        if match is None:
            continue
        remote_id = match.remote_id(backend)
        if not remote_id or remote_id in already_taken:
            continue
        link = match.remote.get(backend)
        project.link_remote(
            backend, remote_id, modified_at=link.modified_at if link else None
        )
        already_taken.add(remote_id)
        linked += 1
    return linked


def _fields_dict(fields: ProjectFields) -> dict[str, Any]:
    """Every canonical field, for a write that carries the whole record."""
    return {name: getattr(fields, name) for name in FIELD_NAMES}
