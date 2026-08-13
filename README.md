# Projection - Project Manager

A project manager for the terminal, built with Textual.

Projection's **local JSON store is the source of record**, and a backend syncs
with it rather than owning it — so it works with no integration configured at
all, which is the default. Two backends are implemented, one active at a time:

| Backend | Reached through | Notes |
|---|---|---|
| *(none)* | — | the default: local file only, no credential, no network |
| Smartsheet | Smartsheet REST API v2, personal access token | adopts a sheet's own column names; no compare-and-swap, so concurrent edits are reconciled by the merge |
| Cloudflare D1 | D1 HTTP API, account API token | Projection owns the table; real compare-and-swap, and the same project id on every device |

Tokens are read from 1Password when a backend first needs one — no OAuth flow, no
MCP server in the data path. Press `,` to choose, create, or connect a backend.
See `docs/ROADMAP.md` for what is planned beyond these two.

## Architecture

**Modules:**
- `projection/panel.py` — `ProjectsPanel`, the whole UI as one widget: sidebar (smart
  lists + statuses), project list, workers for loading and syncing. Its styles
  are scoped `DEFAULT_CSS`, so mounting it elsewhere cannot restyle the host.
- `projection/screen.py` — `ProjectsScreen`, a thin frame: header, panel, footer.
- `projection/app.py` — the app shell: theme, keymap, command palette, default screen.
- `projection/views/` — edit modal, help, summary review; each carries its own styles.

### Embedding the panel

`ProjectsPanel` is a plain widget, so another Textual app can mount it:

```python
from tui.panel import ProjectsPanel
from tui.smartsheet_api import SmartsheetClient

yield ProjectsPanel(SmartsheetClient())
```

It brings its own styles, bindings, and workers, and pushes its own dialogs. It
does not touch the host's theme, and it does not close the client it was handed —
whoever created the client owns it. The Smartsheet token is fetched when the
panel mounts, not at import, so hosting it costs nothing until it is opened.

## Setup

### Prerequisites
- Python 3.11+ and the `uv` package manager
- **1Password CLI (`op`)**, signed in, holding whichever API token your backend
  needs — a Smartsheet personal access token or a Cloudflare token with D1 edit
  permission. Projection has no idea where you keep it: name the reference as
  `token_ref` in that backend's config table (setup asks for it), or export
  `SMARTSHEET_API_KEY` / `CLOUDFLARE_API_TOKEN` as a break-glass path. **Only if
  you sync at all** — with no backend configured, nothing here needs a credential.
- **Claude Code** installed and on `PATH` — only if you use the `exec-summary`
  hook example, which uses it for drafting. Nothing else needs it.

### Installation
```bash
git clone https://github.com/7robots/projection.git
cd projection
uv sync
```

User-configurable settings live in `~/.config/projection/config.toml` (created
with defaults on first run).

## Usage

```bash
./projection.sh
```

**First run** opens the backend wizard once. Choose:

- **Local only** — the default. Your projects live in the local store and nothing
  is synced. No credential, no network, no setup.
- **Smartsheet** — either *create* a sheet (Projection makes one with the columns
  it needs) or *connect* an existing one by its sheet id. If that sheet's columns
  have their own names, Projection asks you to match them up, offering the sheet's
  real column titles.

Press `,` any time to change it, including turning syncing back off. Connecting a
backend to a store you have already been using pushes that work up: projects with
the same title as an existing row are matched to it rather than duplicated.

With a backend configured, launching runs `op read <your token_ref>`. If 1Password
is locked you'll get an unlock prompt; unlock it and the TUI continues. With no
backend, nothing is read and nothing is prompted.

**Keybindings:**

| Key | Action |
|-----|--------|
| `n` / `a` | Create new project |
| `Enter` / `e` | Edit selected project |
| `Space` | Toggle Done / In progress |
| `d` / `⌫` | Delete selected project (`y` confirms, `n`/`Esc` cancels) |
| `s` | Star / unstar the selected project (⟳) |
| `c` | Resolve conflicting changes on the selected project (⚠) |
| `/` | Filter the current view (`Esc` clears) |
| `Tab`, `h` / `l`, `←` / `→` | Switch between sidebar and project list |
| `j` / `k`, `g` / `G` | Move / jump within the focused pane |
| `PgUp` / `PgDn`, `Home` / `End` | Page / jump (the selection follows) |
| `Ctrl+P` | Command palette (New / Refresh / Backend / hooks / themes) |
| `r` | Refresh from the backend |
| `,` | Backend setup (create, connect, or local only) |
| *(your key)* | Run a `[[hooks]]` script over your projects |
| `?` | Keyboard reference |
| `q` | Quit |

The layout is a **sidebar + list** (remtui-style): the left pane holds smart
lists — All Projects, Exec Sync (⟳-flagged), and one list per status — with
live counts; the right pane lists the projects in the selected view. Each row
shows the **full status update** (wrapped, never truncated) so updates are
readable without opening the editor, plus assignees, status, and due date
(overdue dates show red).

Footer actions gray out when they don't apply (nothing selected, nothing
sync-flagged).

**Key profiles and remapping** — `[keys]` in `config.toml`:

```toml
[keys]
profile = "vim"            # "default" or "vim"
"project.new" = "n"        # optional per-binding overrides by binding id
```

The `vim` profile adds: `gg`/`G` top/bottom (`g` becomes a prefix),
`Ctrl+D`/`Ctrl+U` half-page, `Ctrl+F`/`Ctrl+B` page, `:` command palette,
`o` new project. Everything in the base layer (`j`/`k`/`h`/`l`, `/`) is
always on.

An override replaces the binding's keys entirely (comma-separate to keep
several, e.g. `"project.new" = "n,a"`). Binding ids: `project.new`,
`project.edit`, `project.done`, `project.delete`, `project.star`,
`project.conflicts`, `view.refresh`, `view.filter`, `view.dismiss-filter`,
`app.setup`, `app.help`, `app.quit`, `nav.up`, `nav.down`, `nav.left`, `nav.right`,
`nav.top`, `nav.bottom`, `nav.switch-pane`, `vim.half-down`, `vim.half-up`,
`vim.page-down`, `vim.page-up`, `vim.palette`, `vim.new`.

**Edit modal:**

| Key | Action |
|-----|--------|
| `Ctrl+S` | Save changes |
| `Ctrl+D` | Mark as Done and save |
| `Ctrl+E` | Open the update note in `$EDITOR` |
| `Escape` | Cancel |

`Assigned To` is a multi-select over the sheet's contact options (space toggles
a name).

### Hooks

A hook runs a script of yours over the project list, on a key you choose.
Projection has no opinion about what it does — it only hands over projects
safely, and optionally lets you approve the result before the script commits it.

```toml
[[hooks]]
id = "exec-summary"
label = "Executive summary"
key = "x"
command = ["~/path/to/script"]   # a list of arguments, never a shell string
input = "starred"                # all | starred | selection | conflicts
mode = "review"                  # fire, or review to approve the draft first
timeout = 240
env = ["ANTHROPIC_API_KEY"]      # nothing ambient is forwarded by default
```

With `mode = "review"` the script is called twice — `--phase=draft` to produce
text, then `--phase=commit` with the text you approved. Cancelling means the
commit phase never runs, which is why anything irreversible belongs there. A hook
with no `key` is still reachable from the command palette (`Ctrl+P`).

The project list arrives as JSON on **stdin**:

```json
{"hook": "exec-summary", "phase": "draft", "text": null,
 "projects": [{"id": "…", "title": "ZTNA", "status": "In progress",
               "assigned": [{"name": "…", "email": "…"}],
               "due_date": "2026-08-20", "note": "…", "starred": true}]}
```

**A hook is contained at the process boundary**, because that payload holds text
other people wrote in a shared backend: argv is a list so nothing is
shell-interpolated, the payload goes on stdin rather than argv, the environment
is an allowlist you extend per hook, the working directory is a scratch dir so no
repo's `.claude/settings.local.json` is in scope, the whole process *group* is
killed on timeout, and Projection's own API token is never forwarded — even if a
hook names it in `env`. A script needing backend access fetches its own
credential.

`examples/exec_summary/` is a complete worked hook: it drafts a two-section
status roll-up from your starred projects with headless `claude -p`, and on
approval writes it to one row of a shared Smartsheet. It was Projection's
built-in `x` key until it moved there — everything about it is specific to one
team. Its README covers the prompt-injection containment its own `claude` call
needs, which is the script's responsibility rather than Projection's.

## Data storage

- **Source of record:** `~/.local/share/projection/projects.json`. Not a cache —
  Projection is fully usable with no backend configured at all.
- **Backend (optional):** whatever `backend` names in config.toml. It is
  reconciled with the store, field by field, and never owns it.

## Configuration

`~/.config/projection/config.toml`:

```toml
# Empty means local-only, which is the default and fully supported.
backend = "smartsheet"

[backends.smartsheet]
sheet_id   = 0                 # no default: an unset sheet is a loud error
sheet_name = "My Projects"     # verified against the sheet before writing
token_ref  = "op://Vault/item/field"   # or export SMARTSHEET_API_KEY

# Only needed when adopting a sheet with its own column names. A sheet
# Projection provisions itself uses the canonical ones and needs no mapping.
[backends.smartsheet.columns]
title   = "Project"
note    = "Update"
starred = "Sync"

# Or sync with Cloudflare D1 instead — one backend is active at a time, and
# keeping both sections means switching does not mean re-entering ids.
[backends.d1]
account_id    = "…"          # in any Cloudflare dashboard URL
database_id   = "…"          # from `wrangler d1 list`
database_name = "projection" # verified against the database
table         = "projects"   # Projection owns this table; its columns are ours
token_ref     = "op://Vault/item/field"   # or export CLOUDFLARE_API_TOKEN

[sync]
poll_interval = 0   # background poll of the backend; 0 = manual `r` only

[status_options]
projects = ["Not started", "In progress", "Blocked", "On Hold", "Done"]

[keys]
profile = "default"   # or "vim"; per-binding overrides also live here
```

Status values must match the sheet's picklist — Smartsheet rejects values outside
it, and a sheet Projection creates gets exactly this list. D1 stores the status as
text, so it accepts whatever you configure.

**Credentials.** Each backend reads its own: `token_ref` names a 1Password
reference, and the matching environment variable (`SMARTSHEET_API_KEY`,
`CLOUDFLARE_API_TOKEN`) is a break-glass override that is announced at startup
when it is in use. Nothing is ever read from a file — a forgotten token file
silently outranks 1Password and survives rotation. Neither token is ever forwarded
to a `[[hooks]]` script, even if one names it in `env`.

## File structure

```
projection/
├── projection/
│   ├── app.py                  # Main Textual application
│   ├── sync.py                 # Sync coordinator (store of record + a backend)
│   ├── local_storage.py        # The JSON store of record
│   ├── smartsheet_api.py       # Async Smartsheet REST client
│   ├── d1_api.py               # Async Cloudflare D1 HTTP client
│   ├── backends/               # Backend protocol + the Smartsheet and D1 backends
│   ├── merge.py                # field-level three-way merge (pure)
│   ├── hooks.py                # running a user script over the projects
│   ├── secrets.py              # Per-backend credentials, from 1Password
│   ├── models.py               # Pydantic data models
│   ├── config.py               # Configuration (TOML)
│   ├── widgets.py              # Project rows, sidebar options, view header
│   ├── projection.tcss         # Stylesheet
│   └── views/
│       ├── edit_modal.py       # Edit / delete-confirm / loading modals
│       ├── conflict_modal.py   # Per-field "mine or theirs" chooser
│       ├── review_modal.py     # Approve a hook's draft
│       ├── setup_modal.py      # Backend setup wizard + column mapping
│       └── help_screen.py      # Keyboard reference
├── docs/ROADMAP.md             # Planned / deferred work
├── tests/                      # pytest suite (uv run pytest)
├── projection.sh               # Launch script
├── run_tui.py                  # TUI entry point
├── pyproject.toml
├── README.md
└── CLAUDE.md
```

## Testing

```bash
uv run pytest
```

The suite is fully offline — no Smartsheet calls, no `op` invocations.

## Troubleshooting

- **"1Password is locked"** — unlock the 1Password app and relaunch. The first
  read after a lock can sit on a Touch ID prompt for a while; Projection waits
  up to 90s.
- **"1Password CLI (`op`) not found"** — install it. As a break-glass, export
  `SMARTSHEET_API_KEY` in your shell for that session; Projection will warn
  that it isn't using 1Password. Do not put it in a file — nothing reads
  `.env`, by design.
- **"Smartsheet rejected the API token (401)"** — the personal access token is
  invalid or expired. Generate a new one in Smartsheet (Account → Apps &
  Integrations → API Access) and update the 1Password item.
- **"has no column named …"** — the sheet layout changed. Projection reads
  columns by title: `Project`, `Due Date`, `Assigned To`, `Status`, `Sync`,
  `Update`.
- **"Refusing to write: configured row … has Category …"** — the IA row moved or
  was re-created. Fix `summary_row_id` in `config.toml`.
- **"Claude CLI not found"** — only affects the exec-summary hook; install
  Claude Code or set `claude_bin` in `~/.config/projection/exec-summary.toml`.

## Key principles

1. **The local store is the source of truth**, and a project's identity is its
   own permanent id — never a row number, a title, or a backend's key.
2. **A backend is optional**, and syncs *with* the store rather than owning it.
3. **Local-first** for instant reads and writes; remote writes go out in the
   background, and a conflict is surfaced rather than auto-resolved.
4. **Direct REST access** with a 1Password-sourced token — no OAuth, no MCP.
5. **A hook's draft is human-reviewed** before the script is allowed to commit it.

## License

Not yet licensed — add one before depending on this.
