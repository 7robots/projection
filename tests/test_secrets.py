"""Tests for loading a backend token from 1Password.

There is no default reference in the package any more, so every test that
expects `op` to be called supplies one — which is what a configured `token_ref`
does in real use.
"""

import subprocess

import pytest

from projection import secrets
from projection.secrets import TokenError


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


REF = "op://Employee/smartsheet-api-key/credential"


def load_smartsheet_token(*, secret_ref: str = REF) -> str:
    """What every caller does now: name the reference, then read it."""
    return secrets.load_token(secrets.SMARTSHEET.with_ref(secret_ref))


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("SMARTSHEET_API_KEY", raising=False)


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("SMARTSHEET_API_KEY", "  from-env  ")

    def boom(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("op should not be called when the env var is set")

    monkeypatch.setattr(secrets.subprocess, "run", boom)
    assert load_smartsheet_token() == "from-env"


def test_reads_from_op(monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        # `op read` always appends a newline.
        return FakeCompleted(stdout="tok-abc123\n")

    monkeypatch.setattr(secrets.subprocess, "run", fake_run)
    assert load_smartsheet_token() == "tok-abc123"
    assert seen["args"] == ["op", "read", REF]


def test_trailing_newline_is_stripped(monkeypatch):
    """A token with trailing whitespace makes Smartsheet reject the header."""
    monkeypatch.setattr(
        secrets.subprocess, "run", lambda *a, **kw: FakeCompleted(stdout="tok\n\n")
    )
    assert load_smartsheet_token() == "tok"


def test_locked_vault_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(
        secrets.subprocess,
        "run",
        lambda *a, **kw: FakeCompleted(returncode=1, stderr="error: authorization timeout"),
    )
    with pytest.raises(TokenError, match="1Password is locked"):
        load_smartsheet_token()


def test_missing_op_binary(monkeypatch):
    def raise_missing(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(secrets.subprocess, "run", raise_missing)
    with pytest.raises(TokenError, match="not found"):
        load_smartsheet_token()


def test_timeout(monkeypatch):
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="op", timeout=30)

    monkeypatch.setattr(secrets.subprocess, "run", raise_timeout)
    with pytest.raises(TokenError, match="unlocked"):
        load_smartsheet_token()


def test_empty_secret(monkeypatch):
    monkeypatch.setattr(
        secrets.subprocess, "run", lambda *a, **kw: FakeCompleted(stdout="   \n")
    )
    with pytest.raises(TokenError, match="empty"):
        load_smartsheet_token()


def test_other_failure_surfaces_stderr(monkeypatch):
    monkeypatch.setattr(
        secrets.subprocess,
        "run",
        lambda *a, **kw: FakeCompleted(returncode=1, stderr="item not found"),
    )
    with pytest.raises(TokenError, match="item not found"):
        load_smartsheet_token()


# ==================== Per-backend credentials ====================


def test_each_backend_reads_its_own_reference(monkeypatch):
    seen = []

    def fake_run(args, **kw):
        seen.append(args)
        return FakeCompleted(stdout="tok\n")

    monkeypatch.setattr(secrets.subprocess, "run", fake_run)
    secrets.load_token(secrets.SMARTSHEET.with_ref(REF))
    secrets.load_token(secrets.D1.with_ref("op://Private/cloudflare/token"))

    assert seen == [
        ["op", "read", REF],
        ["op", "read", "op://Private/cloudflare/token"],
    ]


def test_a_configured_reference_overrides_the_default(monkeypatch):
    seen = []
    monkeypatch.setattr(
        secrets.subprocess,
        "run",
        lambda args, **kw: (seen.append(args), FakeCompleted(stdout="tok\n"))[1],
    )
    secrets.load_token(secrets.SMARTSHEET.with_ref("op://Mine/sheet/token"))
    assert seen == [["op", "read", "op://Mine/sheet/token"]]


def test_neither_backend_carries_a_default_reference():
    """A vault and item name in the package names one person's 1Password."""
    assert secrets.SMARTSHEET.secret_ref == ""
    assert secrets.D1.secret_ref == ""


def test_no_reference_configured_says_what_to_do(monkeypatch):
    """The Smartsheet case, now that the built-in reference is gone."""

    def boom(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("op should not be called with no reference")

    monkeypatch.setattr(secrets.subprocess, "run", boom)
    with pytest.raises(TokenError, match="SMARTSHEET_API_KEY"):
        secrets.load_token(secrets.SMARTSHEET)


def test_no_reference_and_no_env_var_says_what_to_do(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    def boom(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("op should not be called with no reference")

    monkeypatch.setattr(secrets.subprocess, "run", boom)
    with pytest.raises(TokenError, match="CLOUDFLARE_API_TOKEN"):
        secrets.load_token(secrets.D1)


def test_the_d1_env_var_wins_too(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "  cf-token  ")
    assert secrets.load_token(secrets.D1) == "cf-token"
    assert secrets.token_source == "CLOUDFLARE_API_TOKEN environment variable"


def test_every_backend_credential_is_denied_to_hooks():
    """A hook that names one in `env` is making a mistake, not a request."""
    from projection.hooks import DENIED_ENV

    for credential in secrets.ALL_CREDENTIALS:
        assert credential.env_var in DENIED_ENV


# ==================== The plaintext never reaches a traceback ====================
#
# Textual renders an unhandled exception with `show_locals=True`, and a token is
# shorter than Rich's 80-character truncation — so a frame that still held the
# value would print the credential to the terminal.

SECRET = "tok-do-not-print-me"


def _op_returns_the_secret(monkeypatch):
    monkeypatch.setattr(
        secrets.subprocess, "run", lambda *a, **kw: FakeCompleted(stdout=SECRET + "\n")
    )


def _frames(exc):
    """Every frame in an exception's traceback, as (name, locals)."""
    out = []
    tb = exc.__traceback__
    while tb:
        out.append((tb.tb_frame.f_code.co_name, dict(tb.tb_frame.f_locals)))
        tb = tb.tb_next
    return out


def test_an_unexpected_failure_becomes_a_scrubbed_token_error(monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk on fire")

    monkeypatch.setattr(secrets.subprocess, "run", boom)

    with pytest.raises(TokenError) as caught:
        secrets.load_token(secrets.SMARTSHEET.with_ref(REF))

    # The reading frame is not attached at all, so its locals cannot be rendered.
    assert "_read_token" not in [name for name, _ in _frames(caught.value)]
    # And the chain is cut, so the OSError's own frames are not reachable either.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "OSError" in str(caught.value)


def test_a_baseexception_keeps_its_meaning_but_leaves_no_value_behind(monkeypatch):
    """Cancellation and Ctrl-C must not be turned into auth failures."""

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(secrets.subprocess, "run", interrupted)

    with pytest.raises(KeyboardInterrupt) as caught:
        secrets.load_token(secrets.SMARTSHEET.with_ref(REF))

    reading = [locals_ for name, locals_ in _frames(caught.value) if name == "_read_token"]
    assert reading, "the frame should still be in the traceback"
    # The wrapper deliberately lets this through, so the `finally` is what
    # guarantees the frame holds nothing worth printing.
    for key in ("from_env", "proc", "token"):
        assert reading[0][key] is None, f"{key} still holds a value"


def test_a_deliberate_error_keeps_its_frame_but_not_the_value(monkeypatch):
    """A `TokenError` raised on purpose is re-raised as-is, frame and all.

    That is wanted — the message and its line are the useful part — so the
    `finally` scrub is what keeps the frame from holding anything printable.
    """
    monkeypatch.setattr(
        secrets.subprocess, "run", lambda *a, **kw: FakeCompleted(stdout=SECRET + "\n")
    )
    # A stray failure *after* the value was read: nothing in the traceback may
    # still carry it.
    monkeypatch.setattr(secrets, "OP_SOURCE", "1Password")

    assert secrets.load_token(secrets.SMARTSHEET.with_ref(REF)) == SECRET

    monkeypatch.setattr(
        secrets.subprocess, "run", lambda *a, **kw: FakeCompleted(stdout="   \n")
    )
    with pytest.raises(TokenError, match="empty") as caught:
        secrets.load_token(secrets.SMARTSHEET.with_ref(REF))

    reading = [ls for name, ls in _frames(caught.value) if name == "_read_token"]
    assert reading, "a deliberate TokenError keeps its own frame"
    for key in ("from_env", "proc", "token"):
        assert reading[0][key] is None, f"{key} still holds a value"


def test_the_token_is_still_returned_after_scrubbing(monkeypatch):
    """The scrub runs in a `finally`, so it must not eat the return value."""
    _op_returns_the_secret(monkeypatch)
    assert secrets.load_token(secrets.SMARTSHEET.with_ref(REF)) == SECRET
    assert secrets.token_source == secrets.OP_SOURCE


def test_the_env_path_still_returns_and_records(monkeypatch):
    monkeypatch.setenv("SMARTSHEET_API_KEY", f"  {SECRET}  ")
    assert secrets.load_token(secrets.SMARTSHEET) == SECRET
    assert secrets.token_source == secrets.SMARTSHEET.env_source
