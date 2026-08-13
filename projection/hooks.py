"""Hooks: running a user's own script over the project list.

Projection's core knows how to keep projects. What you *do* with them — draft a
status summary, push a roll-up somewhere, file a ticket — is yours, and lives in a
script rather than in here. The exec-summary flow that used to be built in is now
one instance of this facility; see `examples/exec_summary/`.

A hook is invoked in one or two phases:

- **draft** — Projection writes a JSON payload to the script's stdin and reads its
  stdout. In `mode = "fire"` that output is just reported.
- **commit** — only in `mode = "review"`. The draft is shown for editing and
  approval first, and the approved text comes back on stdin for the script to do
  something irreversible with. Cancelling means the second call never happens.

Two phases exist so the destination write stays in the script while the *review*
stays in the TUI, where it can be seen and cancelled.

Every invocation is locked down at the process boundary, the same way
`examples/exec_summary/headless.py` locks down its `claude -p` call — and for the
same reason: the payload contains project titles and update text that other
people wrote in a shared backend, so it is untrusted input.

- **argv is a list, never a shell string.** Nothing is interpolated into a shell.
- **The payload goes in on stdin**, keeping it out of `ps` output and clear of
  `ARG_MAX`.
- **The environment is an explicit allowlist.** A hook gets no ambient secrets;
  names it actually needs are opted into per hook with `env = [...]`.
- **Projection's own credential is never forwarded**, even if a hook asks for it
  by name. A script that needs backend access fetches its own.
- **The child runs in a scratch directory**, so no repo's `.claude/settings*.json`
  (and its Bash allowlist) is in scope.
- **Every call is bounded** by the hook's timeout.

If you loosen any of this, read `payload()` first: it is a direct
prompt-injection surface for any hook that feeds an LLM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
from asyncio.subprocess import PIPE
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from . import secrets
from .models import FIELD_NAMES, Project

# Which projects a hook is given.
INPUT_CHOICES: tuple[str, ...] = ("all", "starred", "selection", "conflicts")

# What happens with the script's output.
MODE_FIRE = "fire"
MODE_REVIEW = "review"
MODES: tuple[str, ...] = (MODE_FIRE, MODE_REVIEW)

PHASE_DRAFT = "draft"
PHASE_COMMIT = "commit"

# Environment variables always forwarded: enough for a script to find its
# interpreter, its own config, and a terminal, and nothing else.
_ENV_BASE = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
)

# Never forwarded, whatever a hook's `env` list says. Projection's backend
# credentials belong to Projection; a hook that needs backend access reads its own
# from 1Password. Naming one in `env` is a mistake, not a request.
#
# Derived from the credential list rather than typed out again, so adding a
# backend cannot quietly leave its token forwardable to user scripts.
DENIED_ENV = frozenset(c.env_var for c in secrets.ALL_CREDENTIALS)


# Seconds to wait for a killed hook to be reaped. Bounded: the point of the
# timeout is that the UI stops waiting, so reaping must not reintroduce a wait.
_REAP_SECONDS = 2.0


def _kill_tree(proc: "asyncio.subprocess.Process") -> None:
    """Kill the hook and anything it started.

    Killing only the direct child is not enough. A grandchild inherits the
    child's stdout/stderr, so the pipes stay open after the child dies and
    `proc.wait()` blocks until the *grandchild* exits — a hook that backgrounds
    anything would sit past its own timeout, which is the exact failure the
    timeout exists to prevent. The child is spawned with `start_new_session=True`
    so the whole group can be signalled at once.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or no process group to speak of.
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class HookError(Exception):
    """A hook invocation failed."""


@dataclass(frozen=True)
class Hook:
    """One configured `[[hooks]]` entry."""

    id: str
    command: tuple[str, ...]
    label: str = ""
    key: str = ""
    input: str = "all"
    mode: str = MODE_FIRE
    timeout: float = 120.0
    # Environment variable names to forward beyond the base set. Explicit, so a
    # hook that needs an API key says so rather than inheriting the world.
    env: tuple[str, ...] = ()
    # Shown in the review dialog when mode = "review".
    review_title: str = ""

    @property
    def display(self) -> str:
        return self.label or self.id

    @property
    def wants_review(self) -> bool:
        return self.mode == MODE_REVIEW

    @property
    def action(self) -> str:
        """The Textual action string that runs this hook."""
        return f"run_hook('{self.id}')"


def select_projects(
    hook: Hook,
    *,
    projects: Sequence[Project],
    selected: Optional[Project] = None,
) -> list[Project]:
    """The projects a hook should receive, per its `input` setting."""
    if hook.input == "starred":
        return [p for p in projects if p.is_starred]
    if hook.input == "conflicts":
        return [p for p in projects if p.has_conflicts]
    if hook.input == "selection":
        return [selected] if selected is not None else []
    return list(projects)


def project_json(project: Project) -> dict[str, Any]:
    """One project as a hook sees it.

    Canonical field names, and the local id so a script can correlate across
    runs. Deliberately not `model_dump()`: merge bases, per-field timestamps and
    backend keys are Projection's bookkeeping, and a stable, small payload is
    easier to write a script against.
    """
    fields: dict[str, Any] = {}
    for name in FIELD_NAMES:
        value = getattr(project.fields, name)
        if name == "assigned":
            value = [{"name": p.name, "email": p.email} for p in value]
        fields[name] = value
    return {"id": project.id, **fields}


def payload(
    hook: Hook,
    *,
    phase: str,
    projects: Sequence[Project],
    text: Optional[str] = None,
) -> str:
    """The JSON a hook receives on stdin.

    **This is untrusted content.** Titles and notes come from a shared backend
    that other people write to. A hook that puts it in an LLM prompt is a
    prompt-injection surface; that is the hook author's problem to contain, and
    `examples/exec_summary/headless.py` shows how.
    """
    return json.dumps(
        {
            "hook": hook.id,
            "phase": phase,
            "projects": [project_json(p) for p in projects],
            "text": text,
        },
        indent=2,
    )


def child_env(hook: Hook) -> dict[str, str]:
    """The minimal environment for a hook's subprocess."""
    allowed = set(_ENV_BASE) | {
        name for name in hook.env if name not in DENIED_ENV
    }
    return {k: v for k, v in os.environ.items() if k in allowed}


async def run_hook(
    hook: Hook,
    *,
    phase: str,
    projects: Sequence[Project],
    text: Optional[str] = None,
) -> str:
    """Run a hook and return its stdout.

    Args:
        hook: the configured hook.
        phase: `"draft"` or `"commit"`.
        projects: the projects to hand over.
        text: the approved text, on the commit phase.

    Raises:
        HookError: on a missing command, non-zero exit, or timeout.
    """
    args = [*hook.command, f"--phase={phase}"]

    # A scratch cwd keeps any repo's .claude/settings.local.json -- and its Bash
    # allowlist -- out of a child's settings chain.
    with tempfile.TemporaryDirectory(prefix="projection-hook-") as sandbox:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
                cwd=sandbox,
                env=child_env(hook),
                # Its own process group, so a timeout can take out anything the
                # hook started -- and so the hook cannot reach the terminal.
                start_new_session=True,
            )
        except FileNotFoundError:
            raise HookError(
                f"{hook.command[0]!r} not found. Check the `command` for the "
                f"{hook.id!r} hook in config.toml."
            )
        except OSError as e:
            raise HookError(f"Could not start the {hook.id!r} hook: {e}")

        body = payload(hook, phase=phase, projects=projects, text=text)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(body.encode()), timeout=hook.timeout
            )
        except asyncio.TimeoutError:
            _kill_tree(proc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_REAP_SECONDS)
            raise HookError(
                f"The {hook.id!r} hook timed out after {hook.timeout:g}s"
            )

    if proc.returncode != 0:
        detail = (stderr or b"").decode(errors="replace").strip()
        raise HookError(
            detail[:500] or f"the {hook.id!r} hook exited with {proc.returncode}"
        )

    return (stdout or b"").decode(errors="replace")
