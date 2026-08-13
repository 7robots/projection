"""Tests for the headless `claude -p` wrapper using a stub binary."""

import os
import stat

from pathlib import Path

import pytest

import headless


def _make_stub(tmp_path, body: str) -> str:
    path = tmp_path / "claude_stub.sh"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


# ---- subprocess lockdown -------------------------------------------------
#
# The prompt carries update text written by other people in a shared sheet, so
# these are security properties, not preferences.


# The stub writes its dumps next to itself ($0's directory) rather than via an
# env var — the child's environment is scrubbed, which is the point.
_DUMP_ARGS = 'printf "%s\\n" "$@" > "$(dirname "$0")/args.txt"'
_DUMP_STDIN = 'cat > "$(dirname "$0")/stdin.txt"'
_DUMP_CWD = 'pwd > "$(dirname "$0")/cwd.txt"'
_DUMP_ENV = 'env > "$(dirname "$0")/env.txt"'
_OK = "echo '{\"result\":\"ok\"}'"


async def test_all_tools_are_disabled(tmp_path, monkeypatch):
    """Without this the child gets the default tool set, not no tools."""
    stub = _make_stub(tmp_path, f"{_DUMP_ARGS}; {_OK}")
    await headless.run_claude("hi", claude_bin=stub, timeout=30)
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert "--tools" in args
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args


async def test_prompt_goes_on_stdin_not_argv(tmp_path, monkeypatch):
    """Keeps the prompt out of `ps` output and clear of ARG_MAX."""
    stub = _make_stub(tmp_path, f"{_DUMP_ARGS}; {_DUMP_STDIN}; {_OK}")
    await headless.run_claude("SECRET_PROMPT_TEXT", claude_bin=stub, timeout=30)
    assert "SECRET_PROMPT_TEXT" not in (tmp_path / "args.txt").read_text()
    assert (tmp_path / "stdin.txt").read_text() == "SECRET_PROMPT_TEXT"


async def test_token_is_absent_from_the_real_child_process(tmp_path, monkeypatch):
    """End-to-end check that the scrubbed env is what actually gets passed."""
    stub = _make_stub(tmp_path, f"{_DUMP_ENV}; {_OK}")
    monkeypatch.setenv("SMARTSHEET_API_KEY", "tok-should-not-propagate")
    await headless.run_claude("hi", claude_bin=stub, timeout=30)
    assert "tok-should-not-propagate" not in (tmp_path / "env.txt").read_text()


async def test_smartsheet_token_is_not_in_the_child_environment(monkeypatch):
    monkeypatch.setenv("SMARTSHEET_API_KEY", "tok-should-not-propagate")
    monkeypatch.setenv("SOME_OTHER_SECRET", "also-not")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env = headless._child_env()
    assert "SMARTSHEET_API_KEY" not in env
    assert "SOME_OTHER_SECRET" not in env
    assert "PATH" in env


async def test_claude_config_still_reaches_the_child(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "needed-by-the-cli")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere")
    env = headless._child_env()
    assert env["ANTHROPIC_API_KEY"] == "needed-by-the-cli"
    assert env["CLAUDE_CONFIG_DIR"] == "/somewhere"


async def test_runs_outside_the_repo(tmp_path, monkeypatch):
    """A repo cwd would pull in .claude/settings.local.json and its allowlist."""
    stub = _make_stub(tmp_path, f"{_DUMP_CWD}; {_OK}")
    await headless.run_claude("hi", claude_bin=stub, timeout=30)
    used = (tmp_path / "cwd.txt").read_text().strip()
    assert "projection-draft-" in used
    # The repo root is the package's parent; splitting the path on a package
    # name would silently stop meaning anything if the package were renamed.
    repo_root = Path(headless.__file__).resolve().parents[1]
    assert not used.startswith(str(repo_root))


async def test_returns_result(tmp_path, monkeypatch):
    stub = _make_stub(tmp_path, "echo '{\"result\":\"hello\",\"is_error\":false}'")
    out = await headless.run_claude("hi", claude_bin=stub, timeout=30)
    assert out == "hello"


async def test_is_error_raises(tmp_path, monkeypatch):
    stub = _make_stub(tmp_path, "echo '{\"result\":\"boom\",\"is_error\":true}'")
    with pytest.raises(headless.HeadlessError):
        await headless.run_claude("hi", claude_bin=stub, timeout=30)


async def test_nonzero_exit_raises(tmp_path, monkeypatch):
    stub = _make_stub(tmp_path, "echo 'nope' 1>&2; exit 3")
    with pytest.raises(headless.HeadlessError):
        await headless.run_claude("hi", claude_bin=stub, timeout=30)


async def test_missing_binary_raises():
    with pytest.raises(headless.HeadlessError):
        await headless.run_claude(
            "hi", claude_bin="/nonexistent/claude-xyz", timeout=30
        )
