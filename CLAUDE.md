# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

Projection - a terminal project manager, built with Textual.

The **local JSON store is the source of record**, and a backend is synced with it
rather than owning it — Projection works with **no backend at all**, which is the
default. Two are implemented, one active at a time: Smartsheet and Cloudflare D1.
See `docs/ROADMAP.md` for the phased history and what remains before publishing.

## Architecture

- **TUI client**: Python/Textual terminal interface (`projection/`)
- **Two data paths, both plain HTTPS**: `smartsheet_api.py` talks to
  `api.smartsheet.com/2.0`, `d1_api.py` to `api.cloudflare.com/client/v4`, each
  with a Bearer token. No OAuth flow, no MCP server, no Worker in front of D1, and
  nothing generative in the read/write path.
- **API tokens**: read from 1Password when a backend first needs one, held in
  memory only. Each backend has its own `Credential` and its own configurable
  `token_ref`. There is **no `.env` support** — `load_dotenv` was removed
  deliberately, and `python-dotenv` is not a dependency. An exported
  `SMARTSHEET_API_KEY` / `CLOUDFLARE_API_TOKEN` still wins as a break-glass path,
  and the TUI announces it when it does. Do not reintroduce file-based credential
  loading: a forgotten file silently outranks 1Password and survives rotation.
- **No sheet is built in.** There are no default sheet ids in the package: a
  built-in id is how a tool ends up shipping one team's schema, and how a config
  that never named a sheet silently reads someone else's. `backend = "smartsheet"`
  without a `sheet_id` is a loud error.
- **Hooks, not features.** What to *do* with projects — draft a status roll-up,
  push it somewhere — is a user script bound to a key (`[[hooks]]`). The
  executive summary that used to be the built-in `x` key now lives in
  `examples/exec_summary/` as the worked example.
- **Identity is local and permanent** (`Project.id`), not the title and not the
  row id. Renames are ordinary field updates; duplicate titles are harmless. A
  backend's key is mapped in `remote[backend].id`, so a project keeps its identity
  across a re-created row or a change of backend.

## Quick Start

```bash
./projection.sh
```

## An adopted sheet, as a worked example

Nothing below is *the* schema — there is no built-in sheet, and a sheet Projection
provisions itself carries the canonical column names. This is the sheet this
developer's own config adopts, kept here because it is the case a `FieldMap`
exists for: an existing sheet whose vocabulary someone else chose. Columns are
read and written **by title**, so renaming one there means updating the mapping in
`[backends.smartsheet.columns]`.

| Column | Type | Notes |
|--------|------|-------|
| `Project` | TEXT_NUMBER | Primary column; the project title |
| `Due Date` | DATE | Stored ISO, displayed M/D/YYYY |
| `Assigned To` | MULTI_CONTACT_LIST | Editable; options come from the column's `contactOptions` |
| `Priority` | PICKLIST (hidden) | **Never written** |
| `Status` | PICKLIST | `Not started`, `In progress`, `On Hold`, `Blocked`, `Done` |
| `Sync` | PICKLIST | `Yes` / `No` — maps to the canonical `starred` bool |
| `Update` | TEXT_NUMBER | The status update text — canonical `note` |
| `Name+Update` | TEXT_NUMBER (hidden) | **Never written** |
| `Last Updated` | MODIFIED_DATE | System column |

Status and Sync have validation on, so only listed values are accepted.
Writes send **only the cells that changed**.

The exec-summary hook writes to a *different* sheet, which is not Projection's
concern at all: which sheet, which row, and how the row is verified before a write
live in `examples/exec_summary/` and its own config
(`~/.config/projection/exec-summary.toml`). Its README documents that target;
none of it is in the package.

## TUI Client

A Python terminal interface built with Textual for viewing and editing projects.

### Keybindings

Layout is remtui-style: a sidebar of smart lists (All Projects, Starred, and
one per status, with counts) on the left, and the project list on the right.
Rows show the full status update text (never truncated).

| Key | Action |
|-----|--------|
| `n` / `a` | Create new project |
| `Enter/e` | Edit selected project (all fields) |
| `Space` | Toggle Done / In progress |
| `d` / `⌫` | Delete selected project (`y` confirms, `n` cancels) |
| `s` | Star / unstar the selected project (⟳) |
| `c` | Resolve conflicting changes on the selected project (⚠) |
| `/` | Filter the current view (`Esc` clears) |
| `Tab`, `h`/`l` | Switch between sidebar and project list |
| `j`/`k`, `g`/`G` | Move / jump within the focused pane |
| `r` | Refresh (reloads from the backend) |
| `,` | Backend setup — create, connect, or turn off |
| *(your key)* | Run a `[[hooks]]` script — `x` is the exec-summary example |
| `?` | Keyboard reference |
| `q` | Quit |

Key profiles: `[keys]` in `config.toml` — `profile = "vim"` adds gg,
Ctrl+D/U/F/B, `:` (palette), `o` (new); per-binding overrides by binding id
(see README). Footer actions gray out when inapplicable; Ctrl+P palette
includes New / Refresh / Summary.

#### Edit Modal Keybindings

| Key | Action |
|-----|--------|
| `Ctrl+S` | Save changes |
| `Ctrl+D` | Mark as Done and save |
| `Ctrl+E` | Open in external editor ($EDITOR, defaults to vim) |
| `Escape` | Cancel |

`Assigned To` is a `SelectionList` over the sheet's contact options; names are
resolved back to emails before writing.

### Hooks

`[[hooks]]` runs a script of yours over the project list, on a key of your
choosing. Projection has no opinion about what the script does; it only knows how
to hand it projects safely and, optionally, to let you approve the result first.

```toml
[[hooks]]
id = "exec-summary"
label = "Executive summary"
key = "x"
command = ["~/path/to/script"]   # a list, never a shell string
input = "starred"                # all | starred | selection | conflicts
mode = "review"                  # fire, or review to approve the draft
timeout = 240
env = ["ANTHROPIC_API_KEY"]      # nothing ambient is forwarded by default
```

Keys are bound **per instance** in `ProjectsPanel._bind_hooks`, because
`BINDINGS` is a class attribute fixed at import while hooks come from config read
later. A hook asking for a key something else already owns is *refused and
announced* rather than shadowing it — a hook quietly taking `d` would remove
delete with no indication why. A hook needs no key at all: the command palette
lists every one.

**Two phases, and why.** `mode = "review"` invokes the script twice:

| Phase | stdin | then |
|---|---|---|
| `--phase=draft` | `{hook, phase, projects: [...], text: null}` | its stdout is shown for editing |
| `--phase=commit` | the same, `text` = the approved draft | it does the irreversible thing |

Cancelling means the commit phase never runs. That split keeps the destination
write inside the script while the moment a human can say no stays in the TUI.

**Containment** (`hooks.py`) — the payload holds titles and notes other people
wrote in a shared backend, so it is untrusted:

| Control | Why |
|---|---|
| argv is a list | nothing is interpolated into a shell |
| payload on stdin | out of `ps` output and clear of `ARG_MAX` |
| env allowlist | a hook gets no ambient secrets; names it needs are opted into per hook |
| `DENIED_ENV` | Every backend credential (`SMARTSHEET_API_KEY`, `CLOUDFLARE_API_TOKEN`) is never forwarded *even if a hook asks for it* — and the set is derived from `secrets.ALL_CREDENTIALS`, so a new backend cannot be forgotten. Projection's credential is Projection's; a hook fetches its own |
| scratch cwd | no repo's `.claude/settings*.json` (and its Bash allowlist) is in scope |
| `start_new_session=True` + `killpg` | a timeout kills the whole group. Killing only the child leaves a grandchild holding the pipes, so `proc.wait()` blocks until *it* exits — a hook that backgrounds anything would sit past its own timeout, which is the failure the timeout exists to prevent |

`tests/test_hooks.py` asserts each of these. Reviewing a draft does **not**
mitigate prompt injection in a hook that feeds an LLM: any tool call the model
made has already happened before a character reaches the screen. That containment
belongs in the script — `examples/exec_summary/headless.py` is the worked example,
and `tests/test_headless.py` asserts its controls.

### Authentication

The TUI needs the 1Password CLI signed in. `projection/secrets.py` runs `op read` at
launch (90s timeout to allow for an unlock prompt) and never writes the token
to disk or logs. If `op` fails with an authorization timeout, the app is locked
— unlock it and relaunch.

### Configuration

User-configurable values live in `~/.config/projection/config.toml` (created
with defaults on first run). The backend and its sheet, the column mapping,
`[sync] poll_interval`, statuses, key bindings, and `[[hooks]]`.

`config.py` exposes a **`Config` object**, read by `Config.load()` and handed to
whoever needs it — not module-level constants. Importing the package reads
nothing and writes nothing; a test pins that in a subprocess. Three reasons the
globals had to go: a setup wizard has to be able to write config.toml and
*reload* it, per-backend settings need structure rather than a flat namespace,
and a bad value has to be reportable instead of a `ValueError` traceback during
import. `ProjectsApp` reads it once and passes it down through `ProjectsScreen`
to `ProjectsPanel`; the panel falls back to `Config.load()` for hosts (librarian)
that have no reason to know about Projection's config file.

**A value that can't be used never raises.** It falls back to the built-in
default and is recorded in `Config.load_error`, which the panel announces at
mount — the config is built inside `compose()` when embedded, where an exception
takes out the whole modal rather than one setting. It is never silent either:
the built-in defaults include *sheet ids*, so a quiet fallback means reading
someone else's sheet.

That promise extends past *reading* the file to *using* it. `build_backend`
legitimately raises — unknown backend name, Smartsheet with no sheet id, an
unusable D1 table name — so `ProjectsPanel._build_backend` catches `BackendError`,
falls back to local-only, and records `_backend_error` for `on_mount` to announce.
Before that, a single mistyped key in `[backends.*]` killed the whole embedded
modal (and crashed the standalone app at launch), which is the exact failure this
paragraph claims cannot happen. Tests parametrize all three shapes.

What stays module-level in `config.py`: `STATUS_COLORS`, `STATUS_ICONS`,
`STATUS_RANK`, `DONE_STATUS` and the `status_*` helpers. They have no TOML keys
behind them — they are presentation constants, which is why `models.py` and
`widgets.py` can import them without a `Config` in hand.

Anything naming a write target is a **required argument**, not a defaulted one:
`SmartsheetBackend(sheet_id=…)` and `SyncCoordinator(remote=…)`. A default sheet
id in a module is how a stale value writes into the wrong row, and a defaulted
`claude_bin` is a second copy of a config value free to drift from the one
config.toml sets. (`sheet_id=0` is the one legal exception, and it means "nothing
chosen yet": every read and write path refuses it, so the only useful thing such a
backend can do is `provision()`.) `tests/conftest.py` keeps the suite off the real
config and data directories, since the embed path calls `Config.load()`.

**Writing config.toml.** `Config.save()` regenerates the file from the object
rather than patching it, so what lands on disk is exactly what the next `load()`
reads back — the property setup depends on, since it writes a config and then
rebuilds the backend from it. Consequences to keep in mind:

- Comments a user hand-wrote are **not** preserved, so the previous file is copied
  to `config.toml.bak` first. The write itself is atomic, because a half-written
  file parses as "all defaults" and for `backend` that means silently going
  local-only.
- Everything must round-trip, including `[[hooks]]`. A test pins load → save →
  load; losing a hook entry to a backend change would take a user's own script off
  its key with nothing said.
- `Config.source` records where it was read from, and `save()` writes back there.
  Without it, setup inside a test — or against a config elsewhere — would rewrite
  the real file.
- Statuses are the one setting a load *adds* to (`_status_options` appends
  built-ins the file left out), so a narrowed list is not identity across a
  round trip. That is deliberate: a file predating a status must not hide it.

### Data Storage

**The local JSON store is the source of record.** `~/.local/share/projection/`
is not a cache: Projection is meant to work with no backend configured at all,
and a backend *syncs with* the store rather than owning it. A backend is
reconciled in the background (poll per `[sync] poll_interval`, default 0 =
manual `r` refresh only); writes go out immediately.

**Identity is local.** `Project.id` is minted once and never changes. A backend's
own key — a Smartsheet row id, a D1 primary key — lives in `remote[backend].id`,
so a row that vanishes and returns under a new id is still the same project, and
switching backends is a mapping change rather than a data migration. This is also
why the write path got *simpler*: a project's key no longer changes when its row
is created, so a create and a concurrent edit are guarded by one key, not two.

`Project` carries three things a cache never needed:

| Field | For |
|---|---|
| `updated_at` (per **field**) | so two people editing different columns don't conflict |
| `remote[b].base` | the last-synced snapshot — the base of a three-way merge |
| tombstones (in the store) | a missing record is otherwise ambiguous: deleted here, or created elsewhere since the last sync? |

All three are live: `_merge_remote` reconciles field by field against the base
(see **Merging** below) rather than giving either side a blanket win.

Canonical field names are Projection's vocabulary, not a sheet's: `note` (the
Smartsheet's "Update"), `starred` (a real bool, not that sheet's "Yes"/"No"),
`assigned` as `Person` objects **carrying emails**. That last one fixed a real bug
class — the old model kept names only, so writing to a contact column had to
resolve a name from column metadata and *refused the whole write* when it
couldn't.

Rules that exist to prevent silent data loss — change them deliberately:

- **Nothing is discarded to make a version fit.** An older layout is migrated
  (see `_migrate_v2_to_v3`), and the original is copied aside first. A layout
  this build doesn't recognise is moved aside intact, never overwritten. A record
  that fails validation is skipped for display but written back verbatim on the
  next save (`unreadable_items`). The old code answered a version mismatch by
  returning `[]`, which is correct for a cache and data loss for a store.
- **A file does not get to choose where it is moved.** `_aside_path` builds its
  name from `schema-<version>`, and the version is read *out of the file*, so it
  is sanitized (`_UNSAFE_IN_NAME`) and the result is asserted to stay inside the
  data directory. A stored `"schema": "../../tmp/x"` otherwise made the rename
  move the store out of the sandbox — and the process doing the moving is
  Projection's, not the writer's.
- **A migrated `dirty` record gets no merge base.** Its values differ from the
  backend by definition, so snapshotting them as the base would assert "local
  matches remote" and license a later merge to discard the very edit that made it
  dirty. No base means "unknown", which keeps the local copy.
- **A failed write keeps its guard.** Releasing it would let the next refresh
  overwrite the local record with the remote value, discarding whatever the user
  typed, minutes after the error toast had gone.
- **`dirty` is persisted**, so unsynced edits survive quitting, not just a failed
  request.
- **`_merge_remote` keeps the local record's identity.** A remote row arrives with
  a *provisional* id from `_parse_row`; the merge adopts its values onto the local
  record instead of replacing it. Minting a fresh id each fetch would dangle every
  key held by queued work.
- **Deletes leave tombstones, and a tombstone outranks the remote snapshot.**
  This is what makes a delete survive a restart — previously the guard was
  in-memory only, so quitting before the backend accepted the delete brought the
  project back on the next launch. Tombstones are purged only after a *successful*
  fetch, and kept `TOMBSTONE_TTL_DAYS` so another device can still learn of them.
- **Deleting a project mid-create deletes the row the create just made**, rather
  than leaving an orphan to reappear on the next refresh.
- **POST is never retried** on a network error or 5xx (`smartsheet_api.py`).
  Smartsheet may have applied it and lost the response — replaying turns one
  project into two rows. 429 is safe to replay, and PUT/DELETE/GET are
  idempotent.

`SyncCoordinator` guards in-flight writes with a **counted** pending map, so an
overlapping create and edit of the same new project can't unguard each other. An
edit made while a create is still in flight is re-pushed once the key lands.

Note `NAME` in `backends/smartsheet.py` and `_V2_BACKEND` in `local_storage.py`
are the same literal written twice, on purpose: reading a stored file must not
require importing a backend. A test pins them together.

**A record with no link for the active backend is kept, never dropped.** The
merge treats "missing from the snapshot" as "deleted there" only for a record
that was ever *in* there. Every record is unlinked the moment a backend is
attached to an existing local-only install, so without this, connecting a backend
emptied the store — reachable by hand-editing `backend` in config.toml long
before setup made it a menu choice.

### Backends

A backend is somewhere the local store is *mirrored to*. It is never the source
of record, and **there may be none** — `backend = ""` in config.toml is the
default and a fully supported way to run, which is the point of the store being
authoritative. `backends.build_backend(config)` returns `None` for that case, and
`SyncCoordinator` skips every write path rather than pretending.

Exactly one backend is active at a time. The *data model* does not share that
limit — `Project.remote` and the merge bases are keyed per backend — so adding a
second later is a config change, not a migration. Two live at once is deliberately
not taken on: "which backend owns this field" has no good answer yet.

The split that matters is not which service it is, but whether one record can be
written without rewriting the others:

| Backend | Shape | Concurrency |
|---|---|---|
| Smartsheet | row-addressable REST | per-row PUT, `modifiedAt` per row |
| Cloudflare D1 | SQL | real primary keys, transactions |
| Google Sheets | whole range | no real compare-and-swap |
| CSV in R2 | whole object | ETag / `If-Match` gives actual CAS |

The bottom two must declare `row_addressable=False`. `Capabilities` exists so
callers ask instead of assuming.

**Two are implemented: Smartsheet and D1.** The second one is what made the
interface real rather than a description of the first, and it forced four changes
worth knowing about — each one a case Smartsheet simply does not have:

| What D1 needed | Why |
|---|---|
| `create_record(fields, *, project_id=…)` | A SQL primary key can *be* `Project.id`. That makes the create an idempotent upsert, and gives the same project the same id on every device. A Smartsheet row id is assigned for us, so it ignores the argument. |
| `update_record(…, expected_modified_at=…)` + `StaleRecordError` | SQL can refuse a write (`WHERE updated_at = ?`). Smartsheet has no If-Match, which is exactly why `supports_cas=False` there and the merge base carries the weight. |
| `ProbeResult.repairable` | A database with no table is something the backend can fix itself. A sheet missing columns is not — adding columns to someone's sheet is not a decision setup gets to make. |
| `Backend.ensure_ready()` | The panel used to authenticate a *Smartsheet client* directly, which has nothing to say about a backend reading a different credential. Loading credentials is the backend's business. |

Three contract rules in `Backend` exist because breaking them loses data:
`fetch()` sets each record's `remote[name]` link and base and leaves `Project.id`
provisional; **`fetch()` returns the complete set or raises** (an omitted record
reads as "deleted there", and the merge will drop the local copy — so a paginated
read must gather every page); and `update_record()` writes only the fields it is
given.

**Column mapping.** `[backends.smartsheet.columns]` maps canonical field names
onto the target's own column titles, and defaults to canonical names — a sheet
Projection provisions itself needs no mapping at all. The table exists for
*adopting* a sheet that already has its own vocabulary, which Team Projects
does. Note the deliberate asymmetry: the older flat `[smartsheet]` section
defaults to **that sheet's** names (`Project`, `Update`, `Sync`), because that
section only ever described that sheet. `columns.py` holds both maps and imports
nothing, so `config` never has to import a backend.

`backend` is inferred when the key is absent: a config carrying Smartsheet
settings means Smartsheet, so an install predating the key keeps syncing instead
of silently going local-only. The written default config sets `backend = ""`
explicitly, which is what stops a first run and a second run disagreeing.

### The D1 backend

`d1_api.py` is the transport (`POST /accounts/{id}/d1/database/{db}/query` with an
account API token), `backends/d1.py` maps rows to projects. Direct REST, no Worker
in front: a Worker means running a service and owning its auth, and the only thing
it buys is sharing the data with people who should not hold a D1 token.

**Retry safety is per *statement*, not per method.** Every call is a POST, so the
HTTP method says nothing — `query()` therefore takes `retry_safe` explicitly and
each caller states it. `SELECT`, the id-keyed upsert, a plain `UPDATE` and
`DELETE … WHERE id = ?` are all idempotent and replayable; a **compare-and-swap
`UPDATE` is not**, because a replay after the first succeeded finds the stamp
changed and reports a conflict that never happened. Defaulting the flag to True
would make that one case the silent default, so there is no default.

Other decisions in there:

- **`updated_at` is written server-side** (`strftime(…,'now')`). With two devices
  writing, the ordering must come from one clock, and it should not be a laptop's.
- **`RETURNING` is how a write reports that it applied**, with `meta.changes` as a
  fallback. A *missing* count is not zero: reading it as zero would invent a
  conflict, so `QueryResult.changes` returns None for "D1 didn't say".
- **`fetch()` reads with `LIMIT MAX_ROWS + 1` and raises if it fills it.** The HTTP
  API has no cursor, and a silently truncated read is indistinguishable from "the
  rest were deleted" — which the merge would act on.
- **The table name is validated and quoted, not bound.** An identifier cannot be a
  parameter; `_quote_ident` restricts it to letters, digits and underscores.
- **There is no column mapping.** `FieldMap` exists because a *sheet* arrives with
  a vocabulary somebody else chose. This table is Projection's own, so `probe()`
  reports a foreign table of the same name as an error to resolve by choosing a
  different name — not by adding columns to it.

### Credentials

**Nothing that escapes `load_token` carries the plaintext.** Textual renders an
unhandled exception with `show_locals=True` (`textual/app.py`), and a token is
shorter than Rich's 80-character truncation — so a traceback holding a frame where
the value was still bound would print the credential to the terminal, and from
there into scrollback or a screen recording. Two layers, both load-bearing:

- `_read_token` scrubs its own locals in a `finally` (`from_env = proc = token =
  None`). The `return` has already produced its value by then, so the caller is
  unaffected. `CompletedProcess` counts as a holder: its `stdout` *is* the token.
- `load_token` re-raises anything unexpected as a `TokenError` **`from None`**, so
  the reading frame is not attached to the exception at all. `BaseException` is
  deliberately *not* caught — cancellation and Ctrl-C keep their meaning, which is
  why the `finally` exists as well.

A deliberate `TokenError` is re-raised as-is and keeps its frame; that is wanted
(the message and its line are the useful part) and the scrub is what makes it
safe. Tests assert both shapes, including that no frame's locals hold the value.

`secrets.py` holds a `Credential` per backend: a `op://` reference and a
break-glass environment variable (`SMARTSHEET_API_KEY`, `CLOUDFLARE_API_TOKEN`).
The **reference is per-backend config** (`token_ref` under its `[backends.*]`
table), because a published tool cannot know which vault or item name someone
keeps a token in. Two consequences:

- `hooks.DENIED_ENV` is *derived* from `secrets.ALL_CREDENTIALS`, so adding a
  backend cannot quietly leave its token forwardable to user scripts.
- **Neither credential has a default reference.** There was one —
  `op://Employee/smartsheet-api-key/credential`, the last vault-specific string in
  the package — and it is gone: naming a vault and item is the same kind of
  assumption as a built-in sheet id, and for anyone else it sends `op` looking for
  something that is not there. Both `[backends.*]` tables take a `token_ref`, the
  setup wizard asks for one, and the environment variable is the fallback.
  `examples/exec_summary/` names its own, since a hook is never handed
  Projection's.

### Embedding the panel

`ProjectsPanel` is a plain widget, so another Textual app can mount it. One rule
for a host: **hand over nothing but a config, if that.** Which credential to read
comes from Projection's config (`token_ref`), which a host has no reason to know
about — and a client the panel is *given* is used as-is by `build_backend`, so a
helpfully-constructed bare `SmartsheetClient()` produces a panel that cannot find
a token however correct config.toml is. That is exactly how librarian's embed
broke while the standalone app worked.

The panel closes a client it built itself (`_owns_client`), because an embedded
panel is opened and closed repeatedly and nobody else owns that session. A client
it was handed belongs to whoever made it and is left alone.

### Setup: choosing, creating, and adopting a backend

`,` (or the "Projects backend" palette entry) opens `SetupModal`. It is also
offered **once, unprompted**, when `Config.first_run` is true — meaning
config.toml did not exist until this launch. That is the only automatic push: on
every later run setup is a key, because a wizard that reappears is a wizard in the
way. An unreadable config is *not* a first run — it has content, and offering to
overwrite it is the wrong answer to a syntax error.

The flow is `probe()` → `provision()` or adopt → write config → rebuild:

| Step | Where |
|---|---|
| pick a backend (or none), create-or-connect, sheet id/name, `Test` | `views/setup_modal.py` |
| create the target with canonical columns | `SmartsheetBackend.provision()` |
| map a connected target's own column titles | `ColumnMapModal`, fed by `Backend.target_columns()` |
| persist, rebuild the coordinator, reconcile | `ProjectsPanel._setup_flow()` |

Decisions worth not re-litigating:

- **The dialogs write nothing.** They return a `SetupChoice`; the panel does the
  provisioning, the file write, and the rebuild. `Test` is a `probe()` passed in as
  a callback, so no dialog holds a client or a credential.
- **A half-configured backend is never saved.** An unreachable sheet, a cancelled
  mapping, or a mapping that still doesn't fit leaves config.toml untouched —
  writing it would make the *next* launch fail too.
- **`provision()` returns the target's id**, because setup has to persist it: a
  sheet created but not written to config.toml is unreachable. It refuses when a
  sheet is already configured, which would otherwise orphan the first one's rows.
  Its column titles come from the same `FieldMap` the read/write paths use, so
  provisioning and adopting agree by construction.
- **A new target is provisioned, not adopted.** `Capabilities.can_provision` is
  what setup asks; the mapping dialog is only offered when a *connected* sheet
  exists but its columns don't match (`exists=True, ready=False`).
- **The coordinator is replaced, not mutated.** It holds a backend name, in-flight
  guards, and a poll task that all belong to one backend. Setup refuses while
  `SyncCoordinator.busy` — a write landing after the switch would apply its
  bookkeeping to a store that coordinator no longer owns.
- **Attaching a backend runs `adopt()`, not a plain fetch.** See below; an ordinary
  refresh never pushes an already-saved record, so local work would sit there
  looking synced and never arrive.

**Adoption** (`SyncCoordinator.adopt()`) is the one and only first reconciliation:
titles are matched (case-insensitively, trimmed, and only when unambiguous on both
sides) so a project both sides already have doesn't become two rows; the merge
pulls in whatever only the backend had; and everything the backend lacks is pushed,
with local values winning for matched records — you attach a backend to publish the
work you already have. Title matching happens *here and nowhere else*; ordinary
syncing matches on the backend's key. A fetch failure **raises**, because adopting
against an unreachable backend must not look like adopting an empty one, which
would push every record again the moment it came back.

**Local-only never touches 1Password.** `ProjectsPanel._authenticate()` runs only
when a backend exists. It used to run unconditionally, so a fresh install prompted
for a credential it had no use for — and on a machine with no `op` it *failed*,
showing an auth error in place of a perfectly good local store. That was the
out-of-the-box experience for the default configuration.

**Textual trap:** `Select.BLANK` does not exist in Textual 8.2.8 — the sentinel is
`Select.NULL`. The attribute silently evaluates to `False`, so `value=Select.BLANK`
raises `InvalidSelectValueError` at mount and `x is Select.BLANK` is quietly always
false. Two ids also cannot collide inside one screen: the mapping dialog's status
*field* is `#map-status`, so its message line is `#map-message`.

### Merging

`merge.py` is a pure function over three snapshots of one project's fields, kept
out of `sync.py` because this is where the data-loss decisions live.

**Why three-way.** Local and remote values alone tell you they differ but not who
changed what, so the only available policies are "remote wins" (discards the
user's typing) or "local wins" (discards a colleague's). `remote[b].base` — what
both sides last agreed on — makes the question answerable.

**Why per field.** Two people editing different columns of the same project is
the common case on a shared sheet, and it is not a conflict.

| base | mine | theirs | outcome |
|---|---|---|---|
| same | same | same | nothing |
| same | changed | same | keep mine, **base stays** (still unsynced), push it |
| same | same | changed | take theirs, base advances |
| same | changed | changed, equal | converged; take it, base advances |
| same | changed | changed, differs | **conflict** |

A conflict is never auto-resolved. The local value stays on display, the remote
value is recorded in `Project.conflicts` (persisted, so it waits for the user and
not for the process), **nothing is pushed for that field**, and the base is left
alone so the conflict survives until answered. `⚠` marks the row, a transient
"Conflicts" smart list appears while any exist, and `c` opens the per-field
chooser.

Resolving moves the base to *their* value either way, because either way we have
now seen it: "take theirs" makes the field read as agreed, "keep mine" makes it
read as a plain local change that gets pushed. Leaving the base alone would have
the next fetch raise the identical conflict. Editing a conflicted field is also a
decision, and settles it the same way.

Two rules worth not re-deriving: **no base means the local copy wins wholesale**
with no conflicts raised — that is what keeps a v2-migrated dirty record from
conflicting on every field — and **a conflicted field never counts as dirty**,
since dirty means "push me" and pushing is precisely what must not happen yet.

### TUI Code Structure

```
projection/
├── app.py              # App shell: theme, keymap, palette, default screen
├── screen.py           # Thin frame: header, panel, footer
├── panel.py            # ProjectsPanel: the UI and its logic, scoped DEFAULT_CSS
├── widgets.py          # Project rows, sidebar option builders, view header
├── sync.py             # Sync coordinator (local store of record + one backend)
├── local_storage.py    # The JSON store of record: migrations, tombstones
├── smartsheet_api.py   # Async Smartsheet REST v2 client (retries, error mapping)
├── d1_api.py           # Async Cloudflare D1 HTTP client (per-statement retry safety)
├── secrets.py          # Per-backend credentials (op:// refs, env break-glass)
├── hooks.py            # running a user script over the projects (contained)
├── statuses.py         # status colour/glyph/rank (imports nothing)
├── models.py           # Canonical Project model: local identity, field times
├── config.py           # `Config.load()` -> Config object; status display constants
├── columns.py          # canonical field -> column-title maps (imports nothing)
├── merge.py            # field-level three-way merge (pure)
├── backends/
│   ├── base.py         # Backend protocol, Capabilities, FieldMap, Probe/ProvisionResult
│   ├── smartsheet.py   # the Smartsheet backend (rows <-> canonical fields, provision)
│   └── d1.py           # the Cloudflare D1 backend (SQL, CAS, shared identity)
└── views/
    ├── conflict_modal.py # Per-field "mine or theirs" chooser
    ├── edit_modal.py   # Edit/create, delete-confirm, loading modals
    ├── review_modal.py # Approve a hook's draft before it commits
    ├── setup_modal.py  # Backend setup wizard + column mapping
    └── help_screen.py  # Keyboard reference modal
```

## Data Flow

```
┌─────────────────────┐
│        TUI          │
└─────────┬───────────┘
          │ instant read / write — never waits on a network
          ▼
┌──────────────────────────┐
│   Local JSON store       │ ◄──── ~/.local/share/projection/*.json
│   THE SOURCE OF RECORD   │       migrations, tombstones, merge bases
└─────────┬────────────────┘
          │
          │  optional, one at a time, reconciled field by field (merge.py):
          │  writes go out immediately, reads poll per [sync] poll_interval
          │  (default 0 = manual `r`)
          ▼
┌──────────────────────────────────────────────┐
│  a backend — Smartsheet (REST v2) or         │
│  Cloudflare D1 (HTTP API), Bearer token      │
│  from 1Password. Or none at all: the         │
│  default, and fully supported.               │
└──────────────────────────────────────────────┘

   …and separately, on a key of your choosing:

   a hook: your script drafts → you review and edit → your script commits
           (wherever it likes; Projection never learns where)
```

## Key Documents

| Document | Purpose |
|----------|---------|
| `docs/ROADMAP.md` | Phase history, decisions taken, and what remains |
| `README.md` | Setup, keys, config reference, and the hook contract |
| `examples/exec_summary/README.md` | The worked hook, including its own target and containment |

## Testing

```bash
uv run pytest
```

The suite is fully offline: `FakeClient` stubs stand in for the REST client and
`op` is monkeypatched. Never add a test that hits Smartsheet or 1Password.
