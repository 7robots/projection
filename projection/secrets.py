"""Backend credential loading.

1Password is the source of truth. A token is fetched fresh from the `op` CLI when
a backend first needs it, held only in memory — nothing is written to disk and the
value is never logged.

Each backend declares a `Credential`: a `op://` reference and a break-glass
environment variable. The reference is **configurable per backend**
(`token_ref` under its `[backends.*]` table), because a published tool cannot
know which vault or item name someone keeps their token in. The environment
variable stays a fixed name, since it exists for the case where `op` is
unavailable.

A `.env` file is deliberately never read. An exported variable dies with the
shell that set it; a file on disk is forgotten and silently keeps presenting a
credential you thought you had rotated. When an override is in use, the TUI says
so at startup.
"""

from dataclasses import dataclass, replace
from typing import Optional
import os
import subprocess

# `op read` blocks while 1Password shows its unlock / Touch ID prompt, which
# can sit there for a while on the first access after a lock. Generous, but
# still bounded so the TUI never hangs forever.
_OP_TIMEOUT = 90.0


class TokenError(Exception):
    """A backend credential could not be loaded."""


@dataclass(frozen=True)
class Credential:
    """Where one backend's secret comes from, in precedence order."""

    # What to call it in an error message.
    label: str
    # Break-glass environment variable. Checked first.
    env_var: str
    # `op://vault/item/field`. Empty means there is no 1Password reference
    # configured, in which case the environment variable is the only source.
    secret_ref: str = ""

    def with_ref(self, secret_ref: str) -> "Credential":
        """The same credential reading a configured reference instead."""
        return replace(self, secret_ref=secret_ref) if secret_ref else self

    @property
    def env_source(self) -> str:
        return f"{self.env_var} environment variable"


# Neither carries a default reference. A vault and item name in the package is
# the same kind of assumption as a built-in sheet id: it names one person's
# 1Password layout, and for everyone else it sends `op` looking for something
# that is not there. Each backend's reference comes from its own `token_ref`.
SMARTSHEET = Credential(
    label="Smartsheet API token",
    env_var="SMARTSHEET_API_KEY",
)

D1 = Credential(
    label="Cloudflare API token",
    env_var="CLOUDFLARE_API_TOKEN",
)

# Every credential Projection itself loads. `hooks.py` denies these to user
# scripts by name, so a new backend cannot be forgotten there.
ALL_CREDENTIALS: tuple[Credential, ...] = (SMARTSHEET, D1)


# Where the live token came from. Exposed so the TUI can say so out loud —
# an override that isn't announced is one nobody remembers setting. Only one
# backend is active at a time, so one value is enough.
OP_SOURCE = "1Password"

token_source: Optional[str] = None


def load_token(credential: Credential) -> str:
    """Return a backend's token, from the environment or 1Password.

    Records where it came from in `token_source`.

    **Nothing that escapes this function carries the plaintext.** Textual renders
    an unhandled exception with `show_locals=True`, and a token is shorter than
    Rich's 80-character truncation — so a traceback holding a frame where the
    value is still bound would print the credential to the terminal, and from
    there into scrollback or a screen recording. Two layers stop that: `_read`
    scrubs its own locals in a `finally`, and anything unexpected is re-raised
    here as a `TokenError` with the chain cut, so the inner frame is not attached
    at all. `TokenError` messages name *references*, never values.

    Raises:
        TokenError: with an actionable message if the token can't be read.
    """
    try:
        return _read_token(credential)
    except TokenError:
        raise
    except Exception as e:
        # `from None`: the inner frames are what hold the value.
        raise TokenError(
            f"Could not read {credential.label} ({type(e).__name__}). Check "
            f"`op read {credential.secret_ref}`."
            if credential.secret_ref
            else f"Could not read {credential.label} ({type(e).__name__})."
        ) from None


def _read_token(credential: Credential) -> str:
    """Read the credential. **This frame's locals hold the plaintext.**

    Kept separate from `load_token` so the scrubbing and the re-raise are one
    obvious pair rather than something to remember. `BaseException` is
    deliberately not caught — cancellation and Ctrl-C must keep their meaning —
    which is why the `finally` exists as well as the wrapper.
    """
    global token_source

    from_env = proc = token = None
    try:
        from_env = os.environ.get(credential.env_var, "").strip()
        if from_env:
            token_source = credential.env_source
            return from_env

        if not credential.secret_ref:
            raise TokenError(
                f"No {credential.label} configured. Set a 1Password reference "
                f"(`token_ref` in config.toml) or export {credential.env_var}."
            )

        try:
            proc = subprocess.run(
                ["op", "read", credential.secret_ref],
                capture_output=True,
                text=True,
                timeout=_OP_TIMEOUT,
            )
        except FileNotFoundError:
            raise TokenError(
                "1Password CLI (`op`) not found. Install it, or set "
                f"{credential.env_var} in the environment."
            )
        except subprocess.TimeoutExpired:
            raise TokenError(
                "Timed out reading the token from 1Password — is the app waiting "
                "to be unlocked?"
            )

        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()
            if "authorization" in detail.lower() or "not signed in" in detail.lower():
                raise TokenError(
                    "1Password is locked. Unlock the app and relaunch Projection."
                )
            raise TokenError(
                f"`op read {credential.secret_ref}` failed: {detail[:200]}"
            )

        # `op read` appends a newline; an Authorization header with trailing
        # whitespace is rejected by both APIs.
        token = (proc.stdout or "").strip()
        if not token:
            raise TokenError(f"{credential.secret_ref} is empty in 1Password.")
        token_source = OP_SOURCE
        return token
    finally:
        # Rebinding clears what a rendered traceback would show. The `return`
        # statements above have already produced their value by the time this
        # runs, so the caller still gets the token — only the frame is emptied.
        # `CompletedProcess` counts: its `stdout` *is* the credential.
        from_env = proc = token = None  # noqa: F841 — scrubbing, not dead code
