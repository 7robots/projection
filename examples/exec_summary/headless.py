"""Async wrapper around headless Claude Code (`claude -p`).

Used only to draft the executive-summary text.

The prompt embeds project titles and update text written by other people in a
shared Smartsheet, so it must be treated as untrusted input. Every invocation
is therefore locked down at the process boundary rather than trusting the
prompt's own instructions:

- `--tools ""` disables the entire built-in tool set.
- `--strict-mcp-config` with no `--mcp-config` keeps account-level MCP
  connectors out of scope.
- The child runs in a scratch directory, so no project `.claude/settings*.json`
  (and its Bash allowlist) is ever loaded.
- The environment is an explicit allowlist, so the Smartsheet token and any
  other ambient secret is not handed to the subprocess.
- The prompt goes in on stdin, keeping it off the process's argv (visible to
  other local users via `ps`) and clear of `ARG_MAX`.

If any of these are removed, review `build_prompt` in `ia-summary` first: it is
a direct prompt-injection surface, and the payload it interpolates comes from
Projection's hook facility, which documents the same discipline.
"""

import asyncio
import contextlib
import json
import os
import signal
import tempfile
from asyncio.subprocess import PIPE


class HeadlessError(Exception):
    """A headless `claude -p` invocation failed."""


# Environment variables forwarded to the child. Everything else — including a
# break-glass SMARTSHEET_API_KEY and any other ambient secret — is dropped.
# CLAUDE_*/ANTHROPIC_* pass through so the CLI can find its own auth and config.
_ENV_ALLOWLIST = (
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
_ENV_PREFIXES = ("CLAUDE_", "ANTHROPIC_")


def _child_env() -> dict[str, str]:
    """Build the minimal environment for the drafting subprocess."""
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    env.update({
        k: v
        for k, v in os.environ.items()
        if k.startswith(_ENV_PREFIXES) and k != "SMARTSHEET_API_KEY"
    })
    return env


async def run_claude(
    prompt: str,
    *,
    claude_bin: str,
    timeout: float,
    model: str | None = None,
) -> str:
    """Run `claude -p` with all tools disabled and return its result text.

    `claude_bin` and `timeout` are required rather than defaulted: they come
    from `HeadlessConfig`, and a default here would be a second copy of those
    values, free to drift from what the user's config.toml actually sets.

    Args:
        prompt: The prompt to send (delivered on stdin).
        claude_bin: The Claude CLI to run.
        timeout: Seconds before giving up.
        model: Optional model override (e.g. "haiku", "sonnet").

    Raises:
        HeadlessError: on missing binary, non-zero exit, timeout, or error result.
    """
    args = [
        claude_bin,
        "-p",
        "--output-format", "json",
        "--tools", "",           # no built-in tools
        "--strict-mcp-config",   # and no MCP servers
    ]
    if model:
        args += ["--model", model]

    # A scratch cwd keeps the repo's .claude/settings.local.json — and its Bash
    # allowlist — out of the child's settings chain.
    with tempfile.TemporaryDirectory(prefix="projection-draft-") as sandbox:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
                cwd=sandbox,
                env=_child_env(),
                # Its own process group, so the timeout below can take out
                # anything the CLI started rather than only the CLI itself.
                start_new_session=True,
            )
        except FileNotFoundError:
            raise HeadlessError(
                f"Claude CLI not found ({claude_bin!r}). Is Claude Code installed and on PATH?"
            )
        except OSError as e:
            raise HeadlessError(f"Could not start the Claude CLI: {e}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Kill the group, not just the child: a grandchild inherits the
            # pipes, so `proc.wait()` would block until *it* exited -- long past
            # the timeout. Reaping is itself bounded for the same reason.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            raise HeadlessError("Headless Claude call timed out")

    if proc.returncode != 0:
        detail = (stderr or b"").decode(errors="replace").strip()
        raise HeadlessError(detail[:500] or f"claude exited with {proc.returncode}")

    try:
        data = json.loads((stdout or b"").decode(errors="replace"))
    except json.JSONDecodeError as e:
        raise HeadlessError(f"Could not parse claude output: {e}")

    if data.get("is_error"):
        raise HeadlessError(str(data.get("result", "headless error")))

    return data.get("result", "")
