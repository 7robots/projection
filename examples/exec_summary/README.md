# exec-summary hook

Drafts a two-section status roll-up from your **starred** projects, shows it for
review, and on approval writes it to one row of a shared Smartsheet.

This was built into Projection as the `x` key until it moved here. Everything
about it is specific to one team — which sheet, which row, that the row is found
by the value `IA`, that a `Status Updated?` checkbox gets ticked — and none of
that belongs in a tool other people install. It is kept as the worked example of
the `[[hooks]]` facility.

## Setup

```bash
cp config.example.toml ~/.config/projection/exec-summary.toml
$EDITOR ~/.config/projection/exec-summary.toml     # your sheet, row, category
```

Then in `~/.config/projection/config.toml`:

```toml
[[hooks]]
id = "exec-summary"
label = "Executive summary"
key = "x"
command = ["~/GitHub/projection/examples/exec_summary/ia-summary"]
input = "starred"          # only starred projects are ever included
mode = "review"            # draft -> you approve -> commit
timeout = 240
review_title = "Review executive summary"
```

Requires the `claude` CLI on `PATH` for drafting, and the 1Password CLI signed in
for the Smartsheet write (it reads its own credential — Projection never hands a
hook its own).

## How it runs

Projection calls the script twice, with the project list as JSON on stdin:

| Phase | stdin | stdout |
|---|---|---|
| `--phase=draft` | `{hook, phase, projects: [...], text: null}` | the draft text |
| `--phase=commit` | the same, with `text` set to the approved draft | a confirmation |

Cancelling the review means `--phase=commit` never runs. That is why the
irreversible write lives only in that phase.

## Security

The projects on stdin are **untrusted**: titles and update text come from a sheet
other people write to, and `build_prompt` interpolates them *after* the
instructions — the most injection-favourable position. The review step does not
mitigate this. It gates the Smartsheet write, but any tool call would already
have fired before you saw a character.

Containment is at the process boundary, in `headless.py`: no tools, no MCP
servers, a scratch working directory, an allowlisted environment, and the prompt
on stdin. Read the table in that file's docstring before changing how the model is
invoked. `tests/test_headless.py` asserts each control.

Projection applies the same discipline to the hook itself — argv as a list, JSON
on stdin, an allowlisted environment, a scratch cwd, a bounded timeout, and its
own backend credential never forwarded. See `projection/hooks.py`.
