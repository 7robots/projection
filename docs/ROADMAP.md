# Projection Roadmap

Single source of truth for planned and deferred work.

## Recently completed

**Smartsheet cutover (2026-08-07)** — replaced Google Sheets with the
**Team Projects** Smartsheet as the source of truth, and swapped the whole
data path from headless `claude -p` + MCP connectors to the Smartsheet REST
API v2 with a 1Password-sourced token. Also in that change:

- Removed the Claude CoWork skill (`SKILLS.md`, `cowork-plugin/`,
  `.claude/skills/projects.md`) and the Google Sheets adapter (`mcp_storage.py`).
- Removed the personal 1-1 note write. Leadership Roll-up is now the only
  exec-summary destination.
- Identity moved from project **title** to **Smartsheet row id**.
- `Assigned To` became editable (Google Sheets smart chips no longer apply).
- Status enum tracks the sheet: `Completed` → `Done`, `Cancelled` dropped.
- The summary push now also ticks `Status Updated?` on the IA row.

**Review hardening (2026-08-07)** — a security and an architecture review pass
over the cutover found, and this fixed:

- `claude -p` ran with the **default tool set**, not none, and from the repo
  root — so it inherited `.claude/settings.local.json`'s `Bash(python3:*)` and
  `Bash(gh api:*)` grants. Combined with untrusted sheet text in the prompt,
  that was prompt-injection-to-code-execution. Now `--tools ""`,
  `--strict-mcp-config`, scratch cwd, scrubbed env, prompt on stdin.
- POST was retried on network errors and 5xx, which could duplicate a project row.
- Deleting a project mid-create left an orphan row that reappeared on refresh.
- Any failed write was silently overwritten by the next refresh; unsynced edits
  now carry a persisted `dirty` flag and keep their guard.
- `local_id` was regenerated on every fetch, dangling keys held by queued work.
- Unresolvable assignee names were written as "clear the cell".
- A failed initial sync presented as "you have no projects"; a failed refresh
  still reported "Loaded N items".
- Background write tasks held no strong reference and could be GC'd mid-write.
- Local cache writes are now atomic; the summary push verifies sheet name,
  duplicate column titles, and reads the row back after writing.

**Dropped `.env` support (2026-08-07)** — removed `load_dotenv` from
`config.py` and `python-dotenv` from the dependencies. 1Password is now the
only credential store. An exported `SMARTSHEET_API_KEY` remains as a
break-glass path and is announced at startup, but nothing is ever read from a
file: a forgotten token file silently outranks 1Password and survives
rotation. Do not reintroduce file-based credential loading.

## Planned

### Local-first with pluggable backends (2026-08-12, phases 0–6 complete)

The goal, now met: **Projection works out of the box with no data integration at
all**, and an integration *syncs with* the local store rather than replacing it.
Smartsheet is one backend among several rather than the assumption. This is what
made the project publishable, and what makes the panel installable by anyone
embedding it.

The phase notes below are kept as written: they record why each decision went the
way it did, which is worth more than a tidy summary when the next change comes
along.

**Design decisions taken (the author, 2026-08-12):**

| Question | Decision |
|---|---|
| Where first-run setup lives | **A Textual wizard**, not a CLI flow. librarian embeds the panel and there is no terminal to prompt into from there — and the embed is how projection is normally opened. Pinned from librarian's side too (`TestBackendSetupWhileEmbedded`). |
| How setup persists a choice | **Regenerate config.toml** from the `Config` object, keeping the old file as `.bak`. No new dependency, and what lands on disk is exactly what the next `load()` reads. `tomlkit` would preserve comments; not worth a runtime dependency in a project down to textual/httpx/pydantic. |
| How far provisioning goes | **Create *and* adopt.** Creating the sheet is what makes "works out of the box, then connect a backend" true end to end, rather than sending people to Smartsheet's UI mid-setup. |
| How D1 is reached (the author, 2026-08-12) | **The HTTP API directly**, not a Worker in front. A Worker means running a service and owning its auth; what it would buy — sharing the data with people who should not hold a D1 token — is not this backend's case. Revisit only if that changes. |
| Whether `supports_cas` becomes real | **Yes, as detection only.** A refused write refetches and lets the merge decide; it is never retried, and the merge stays the single reconciliation mechanism. Two mechanisms deciding one question is how they come to disagree. |
| Multi-device sync (two machines through one backend) | **In scope.** Already happens on the Smartsheet — a colleague writes to the same sheet — so D1 should have parity. Sets the correctness bar: the merge must be real, not last-write-wins. |
| Several backends active at once | **No — one at a time.** The data model still stores per-backend ids and snapshots, so switching later is not a migration, but field ownership across two live backends is a problem we're not taking on. |
| Conflict granularity | **Field level.** Two people editing different columns of the same project should never conflict. Smartsheet's per-row `modifiedAt` supports it. |
| Rename `update` → `note` | **Yes**, while the store is being versioned anyway. "update" as both noun and verb reads badly throughout. |

**Why the existing `local_storage.py` is a cache, not a store of record** — the
three things phase 1 has to invert:

1. **Identity is Smartsheet's.** `Project.key` is the row id and `local_id` is an
   explicit placeholder until the API assigns one. The local id has to become
   permanent and primary, with `row_id` demoted into a per-backend map.
2. **A schema bump deletes data.** `load()` returns `[]` when the version differs
   and `has_local_data()` calls a stale cache *absent*. Correct for a cache;
   data loss for a store of record. Must migrate instead — and there is live
   state in `~/.local/share/projection/projects.json` to migrate.
3. **`_merge_remote()` gives remote the win.** Simply flipping it is worse: the
   whole point of the shared sheet is that colleagues edit it, so local-always-wins
   would discard their work. Real two-way sync needs a **per-backend base
   snapshot** — base/local/remote is what makes "who changed this?" answerable.
   Plus per-field `updated_at` and real **tombstones** (a missing record is
   otherwise ambiguous: deleted here, or created there since the last sync?).

**A column-name mapping is necessary but not sufficient** — the canonical schema
needs *types*, because Smartsheet's shape is not just differently-named:

- `assigned` is a `MULTI_CONTACT` objectValue, and the local store keeps names
  only — so a write has to resolve name→email from column metadata and
  **refuses entirely** when a name is unknown. Storing `[{name, email}]` locally
  fixes a real bug class and lets a plain-text backend degrade to names.
- Dates round-trip through `to_display_date`/`to_iso_date`; Sheets hands back
  serials, D1 wants ISO text.
- `sync = "Yes"` is a cell artifact in the core model; locally it is a bool.

Canonical fields: `title` (text), `status` (enum), `assigned` (people[]),
`due_date` (date), `note` (longtext), `starred` (bool). Mapping is per backend and
**defaults to the canonical names**, so a sheet Projection *creates* needs no
mapping — the table exists for adopting a sheet that already has its own vocabulary.

**The backend split that matters is not Smartsheet vs D1** — it is
row-addressable vs whole-document:

| Backend | Shape | Concurrency |
|---|---|---|
| Smartsheet | row-addressable REST | per-row PUT, `modifiedAt` per row |
| D1 | SQL | easiest — real PKs, transactions, native `updated_at` |
| Google Sheets | whole range | no real compare-and-swap; lost-update prone |
| CSV in R2 | whole object | ETag / `If-Match` gives actual CAS |

So the interface carries a small **capabilities** record (`row_addressable`,
`supports_cas`, `can_provision`, `typed_contacts`) rather than pretending the four
are alike. D1 needs no new dependency — `httpx` is already here.

**Phases** (each shippable on its own):

- [x] **0. `Config` object** — module-level constants evaluated at import can't
  express per-backend settings and can't be reloaded by a wizard. Done; no
  behavior change, 18 tests added.
- [x] **1. Local store becomes the store of record.** Permanent ids,
  migrate-not-discard, tombstones, per-field `updated_at`, typed fields, base
  snapshot slot. Done: schema v3, verified against a copy of the real 48-project
  store. Notable outcomes beyond the plan:
  - **Deletes are now durable.** A delete that the backend rejected used to come
    back on the next launch (the guard was in-memory only); the tombstone
    survives a restart. This closes two items previously listed under
    "Deferred / possible".
  - **Assignees carry emails.** The old model stored names only, so writing to a
    contact column had to resolve an address from column metadata and refused the
    whole write when it couldn't.
  - **The write path got simpler, not more complex.** Identity no longer changes
    when a row is created, so a create plus a concurrent edit needs one pending
    guard instead of two, and `stamp_row_id`'s two-key dance is gone.
  - Renames: `update` → `note`, `sync="Yes"` → `starred: bool`, and the `s`
    binding is now `project.star` (was `project.sync-flag`) — worth knowing if a
    `[keys]` override ever referenced the old id.
- [x] **2. Backend interface + Smartsheet refitted behind it** + column mapping +
  field-level three-way merge + conflicts surfaced in the UI. Done. Shape:
  `backends/base.py` holds the protocol, `Capabilities`, `FieldMap` and
  `ProbeResult`; `backends/smartsheet.py` is the refitted store (`project_store.py`
  is gone); `merge.py` is the merge as a pure function; `columns.py` is a leaf
  module holding the column maps so `config` never imports a backend.
  - **A backend is optional.** `backend = ""` is the default and fully supported —
    `build_backend` returns None and every write path is skipped. That is the
    milestone that makes the repo publishable, ahead of the hooks work.
  - **`backend` is inferred when absent**, so an install predating the key keeps
    syncing rather than silently going local-only. The written default config
    states `backend = ""` explicitly, which is what stops a first and second run
    disagreeing.
  - **Conflicts are surfaced, never auto-resolved** — `⚠` on the row, a transient
    "Conflicts" smart list, `c` for the per-field chooser, and nothing pushed for
    a conflicted field until it is answered.
  - Found and fixed on the way: `sync_age_seconds` compared a naive
    `datetime.now()` against the now-aware stored stamp, raising `TypeError`
    *inside the repopulate worker* — which takes the panel down. Naive stamps are
    now read as **local** time (v2 wrote `datetime.now()`), not UTC, or every age
    would be off by the UTC offset.

**Known constraint** worth remembering before writing the Sheets or R2 backends:
`fetch()` must return the complete record set or raise. A record the backend holds
but omits reads as "deleted there", and the merge will drop the local copy — so a
paginated read has to gather every page, and a partial result must raise.
- [x] **3. Scriptable hooks; exec-summary moved to `examples/`.** Done. `[[hooks]]`
  runs a user script on a key of your choosing; `hooks.py` contains it; the
  exec-summary flow is `examples/exec_summary/` with `headless.py` and
  `summary_store.py` moved there intact.
  - **The last team-specific things left the package**: `summary_sheet_id`,
    `summary_row_id`, `"IA"`, `Status Updated?`, the prompt — *and* the default
    `projects_sheet_id`. There is now **no built-in sheet at all**; `backend =
    "smartsheet"` without a `sheet_id` is a loud error. That default was the
    original "ships someone else's schema" problem in its purest form.
  - **Caught before it shipped:** the author's config.toml dates from the Google
    Sheets / MCP era and never named a sheet — it relied on that built-in default.
    Tightening the backend inference would have silently dropped him to
    local-only. His config now names the sheet, its column mapping, and the hook
    explicitly (backup at `config.toml.pre-hooks-backup`).
  - **Also fixed:** a hook timeout could be defeated by a grandchild holding the
    pipes — `proc.wait()` blocked until *it* exited, 30s against a 0.3s timeout.
    Now the whole process group is killed. `headless.py` had the identical bug.
  - **Also fixed:** projection's config docstring claimed XDG for a long time
    while only reading `~/.config`, making it the odd one out among the four
    apps. It now honours `XDG_CONFIG_HOME`/`XDG_DATA_HOME` like the others.
  - Incidental: `statuses.py` extracted as a leaf module, because
    `config -> hooks -> models -> config` was a genuine import cycle.
**Live verification (the author, 2026-08-12).** Phases 0–3 exercised against the
real sheet and the real store, in both hosting modes:

| Path | How |
|---|---|
| v2 → v3 migration | ran on first real launch; archive kept beside the store |
| edit an existing project | standalone; confirmed landed in Smartsheet |
| create a new project | embedded in librarian; confirmed landed in Smartsheet |
| `q` closes the embedded panel | librarian's `priority=True` binding wins over projection's `q -> app.quit` |
| store health afterwards | 49 records, 0 dirty, 0 missing row ids, 0 missing merge bases, 0 conflicts |

That last row is the one worth re-checking after any change to the write path: a
create that pushes but never records its merge base looks identical in the UI
while quietly winning every future merge, masking a colleague's edits to that row.

**Still untested by hand**, in order of how much it matters. The phase-4 items are
first because they touch config.toml and the write path, and because the author's
own install is the awkward case: a store with 49 records already linked to the real
sheet, so `,` → Save would run `adopt()` over live data. Re-check the store-health
row above afterwards.

- **Setup against the real config** — opening `,`, saving *the same* Smartsheet
  settings, and confirming `config.toml.bak` holds the old file, the `x` hook and
  its `[keys]` survive the rewrite, and adoption reports nothing to reconcile
  (every record is already linked, so there is nothing to match or push).
- **Creating a sheet** — the one path that writes to Smartsheet's account level
  rather than a row. Worth doing once in a scratch sheet, then deleting it.
- **Turning a backend off and back on** — local-only, add a project, reconnect;
  the new project should be pushed and matched, not duplicated.

- ~~**The `x` hook end to end**~~ — **run 2026-08-14, and it was broken.** The
  script died on its first import: a hook is a subprocess with a scratch working
  directory, so `#!/usr/bin/env python3` got a system interpreter with neither
  `projection` nor httpx. Run from the repository root it worked by accident,
  because the package is a subdirectory there — which is why every check before
  this one passed. It now hands off to the project's own interpreter.

  Verified through the panel first — 12 starred projects → the hook → `claude -p`
  → a two-section draft in the review dialog → cancel, *"nothing committed"* — and
  then **for real, including the commit phase**: the reviewed summary reached the
  roll-up sheet (the author, 2026-08-14). The longest chain in the system is now
  exercised end to end, irreversible step included.
- ~~**Delete**~~ — run live 2026-08-14. The last CRUD path, and the one with
  tombstones behind it.
- **Conflict resolution** — the one thing left, and **deliberately deferred**
  (the author, 2026-08-14): staging it means arranging two writers against the same
  field between two syncs, which is more setup than it is worth right now. It is no
  longer *hard*, at least — point two stores at one D1 database, edit the same
  field in both, and sync; `test_d1_integration.py` already covers two stores
  sharing one project identity, so the harness is a few lines from existing. Worth
  doing before anyone else relies on the merge, since it is the only rule in the
  system whose failure mode is silent.

- [x] **4. Provisioning + first-run setup** — `probe()` → `provision()` → adopt.
  Done. `views/setup_modal.py` holds `SetupModal` (backend, create-or-connect,
  `Test`) and `ColumnMapModal` (per-field pick from the target's *real* column
  titles); `SmartsheetBackend.provision()` creates a sheet with the mapped
  columns; `Config.save()` persists the answer; `SyncCoordinator.adopt()` does
  the first reconciliation. `,` opens it, and a genuine first run
  (`Config.first_run` — config.toml did not exist until this launch) offers it
  once, unprompted. Notable outcomes beyond the plan:
  - **Two data-loss bugs found on the way, both reachable before any of this
    shipped.** `_merge_remote` dropped local records with no link for the active
    backend, so hand-editing `backend = "smartsheet"` into a config that had been
    running local-only *emptied the store* on the first fetch. And a record
    written while local-only is `dirty = False` — rightly, there was nothing to
    be out of step with — so nothing marked it unsynced once a backend appeared;
    adoption now sets it, and a push that fails leaves an honest flag.
  - **Local-only never touches 1Password.** `_load_projects` fetched the token
    unconditionally, so the *default* configuration prompted for a credential it
    had no use for — and on a machine without `op` it failed and showed an auth
    error in place of a perfectly good local store. That was the out-of-the-box
    experience the whole local-first effort was for.
  - **Adoption pushes, and matches by title.** An ordinary refresh never pushes an
    already-saved record, so connecting a backend to existing local work would
    leave it looking synced and never send it. Titles are matched once, here, and
    only when unambiguous on both sides, so a project both sides already have
    doesn't become two rows. Local values win for matched records: you attach a
    backend to publish work you already have.
  - **Nothing half-configured is ever written.** An unreachable sheet, a cancelled
    mapping, or a mapping that still doesn't fit leaves config.toml untouched —
    saving it would make the next launch fail too.
  - **The config writer regenerates the file**, so `[[hooks]]` and everything else
    has to round-trip; a test pins load → save → load, because losing a hook to a
    backend change would take a user's own script off its key silently. The
    previous file is kept as `config.toml.bak`, since hand-written comments do not
    survive.
  - Textual trap worth remembering: `Select.BLANK` does not exist in 8.2.8 (it is
    `Select.NULL`) and silently evaluates to `False`, so `value=Select.BLANK`
    raises at mount and `is Select.BLANK` is quietly always false.
- [x] **5. D1 backend** — the second implementation is what actually validates the
  interface. Done, over the **D1 HTTP API directly** (`d1_api.py` +
  `backends/d1.py`), with no Worker in front: a Worker would mean running a
  service and owning its auth, and the only thing it buys is sharing the data with
  people who should not hold a D1 token. What the second backend forced, each a
  case Smartsheet does not have:
  - **`create_record(…, project_id=…)`** — a SQL primary key can *be*
    `Project.id`. That makes the create an idempotent upsert (a replay after a
    lost response cannot duplicate a project, which an appended sheet row can) and
    gives the same project the same id on every device. Smartsheet ignores it.
  - **`update_record(…, expected_modified_at=…)` and `StaleRecordError`** — real
    compare-and-swap. The coordinator sends the last-seen stamp *only* to a
    backend declaring `supports_cas`, never retries a refused write, and refetches
    so the merge decides. Without it, a concurrent write is a silent lost update
    that leaves the local record looking clean.
  - **`ProbeResult.repairable`** — a database with no table is something the
    backend fixes itself; setup calls `provision()` instead of showing the mapping
    dialog. A sheet missing columns is never repairable: adding columns to
    someone's sheet is not a decision a wizard gets to make.
  - **`Backend.ensure_ready()`** — the panel authenticated a *Smartsheet client*
    directly, which has nothing to say about a backend reading a different
    credential. Loading credentials belongs to the backend.
  - **Retry safety turned out to be per statement, not per method.** Every D1 call
    is a POST, so the method says nothing: `SELECT`, the id-keyed upsert, a plain
    `UPDATE` and `DELETE … WHERE id = ?` are replayable, and a compare-and-swap
    `UPDATE` is not — a replay after the first succeeded finds its own stamp and
    reports a conflict that never happened. `query()` therefore has no default for
    `retry_safe`.
  - **Credentials became per backend** (`secrets.Credential`, `token_ref` in each
    `[backends.*]` table), and `hooks.DENIED_ENV` is now *derived* from that list
    so a new backend cannot leave its token forwardable to user scripts.
  - **Tested against real SQLite.** `test_d1_integration.py` runs the statements
    the backend builds against an in-memory database, so invalid SQL, a column the
    DDL never created, or a rejected `RETURNING` fails in the suite. D1 *is*
    SQLite, which is what makes the substitution fair. A one-off harness also
    exercised the whole path through a stub HTTP server (17/17), including CAS
    refusal, upsert idempotency, tombstones, and two stores sharing one identity.

**Still untested against real Cloudflare** (the suite is offline by design): the
account-level `POST /d1/database` create, a token with the wrong permissions, and
whether `meta.changes` is present in practice — the code treats a missing count as
unknown and relies on `RETURNING`, so both paths work, but only one has been seen.
- [ ] **6. Publish.** Two of the three preparation tasks are **done**
  (2026-08-13); what remains is the decision to flip the repository to public, and
  the one thing that decision needs weighing first — see below.
  - [x] **No vault-specific string left.** `secrets.OP_SECRET_REF` is gone. Both
    credentials now read a `token_ref` from their own `[backends.*]` table, the
    setup wizard asks for one (Smartsheet as well as D1), and the environment
    variable is the fallback. the author's config.toml and exec-summary.toml name
    the reference explicitly, and the hook example resolves its own — a hook is
    never handed Projection's. **Found on the way:** `token_ref` had been added to
    `SmartsheetConfig` and to the writer in phase 5 but never to the *reader*, so a
    correct Smartsheet `token_ref` was silently ignored. Harmless while a default
    existed; it would have broken every Smartsheet install the moment the default
    went. A test pins the whole chain now, config.toml → `Credential` → client.
  - [x] **Team framing out of the package.** `pyproject.toml`'s description, the
    README's tagline, licence line and key principles, `app.py`'s docstrings and
    `SUB_TITLE`, and the stale "the defaults are the IA team's sheets" comment in
    `config.py`. CLAUDE.md's sheet-layout section is now framed as *one adopted
    sheet as a worked example* rather than the schema, with the exec-summary target
    and its row id moved out to the example's own README. Its data-flow diagram was
    also simply **wrong** — still showing Smartsheet as the source of truth and the
    local store as a cache, the inversion phase 1 made — and is redrawn.
  - [x] **Split a public repository out.** Done: this repository is the public one,
    started from a scrubbed tree with no history — the earlier commits held sheet
    ids, a row id, a vault reference and an old Drive path, none of them secrets
    but all of them identifiers pointing at one team's infrastructure. The
    author's own instance continues privately as a fork of this repo, so
    team-specific customizations stay versioned without shipping here. librarian
    can now declare its optional extra against this URL.

- [ ] Later, and no longer on the critical path: Google Sheets and CSV in R2. Both
  are whole-document backends, so they are the ones that test
  `row_addressable=False` — which nothing does yet, and which is where a
  lost-update bug would hide.

Two items under "Deferred / possible" get absorbed by phase 1: **retrying dirty
rows** and **failed deletes only being remembered in-process** are both really
"the local store has no durable intent model", which tombstones and per-field
timestamps give it.

## Cross-cutting work

Three items span all four projects (librarian, remtui, projection, taskpapertui) and are tracked in
**librarian's** `docs/ROADMAP.md` rather than duplicated here:

- **Performance review** — establish a baseline before optimizing; nothing is known to be slow yet.
- **A Rust/ratatui prototype**, strictly conditional on that review. Note the constraint: this
  project's panel is embedded *in-process* by librarian, so rewriting it in Rust would drop it to the
  suspend-and-launch handoff.
- **Security and code review** — **done for this repo** (2026-08-12, see phase 6);
  still outstanding for librarian, remtui and taskpapertui: subprocess construction,
  untrusted text from other people's calendars/reminders reaching filenames and
  rendered output, path handling, and token safety.

## Deferred / possible

- **Live write smoke test in CI.** The suite is deliberately offline. A manual
  end-to-end check (create → edit → delete a scratch row) is worth running after
  changes to `backends/smartsheet.py`. D1 needs it less: `test_d1_integration.py`
  runs the real statements against SQLite, so only the HTTP layer is unexercised.
- **Attachments and comments.** The Smartsheet API exposes both; the TUI shows
  neither.
- **`Priority` column.** Present but hidden in Team Projects, and unused by
  the TUI. Surface it as a sidebar view if it starts being maintained.
- **Rate-limit backoff tuning.** `smartsheet_api.py` retries 429/5xx with linear
  backoff. Smartsheet's published limit is 300 requests/min per token, which the
  TUI is nowhere near, so this is untuned on purpose.
- **Token expiry warning.** The 1Password item carries an `expires` date;
  Projection could warn on launch when it's close.
- **Retrying dirty rows.** A row whose write failed keeps its local copy and is
  flagged `dirty`, but nothing retries it automatically — the user re-saves.
  A retry-on-refresh pass would close that loop. (Phase 1 gave the store the
  durable intent this needs; the retry itself is still to do.)
- ~~**Failed deletes are only remembered in-process.**~~ **Done in phase 1** —
  deletes leave a tombstone that outranks the remote snapshot and survives a
  restart.
- **`--settings` pinning for the drafting subprocess.** The scratch cwd keeps
  project settings out, but pointing `--settings` at a locked-down file would
  stop a future user-level allowlist change from re-widening it.
