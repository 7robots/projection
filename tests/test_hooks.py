"""Tests for the hook facility: selection, payload, containment, invocation."""

import json
import os
import stat
import time
from pathlib import Path

import pytest

from projection.hooks import (
    DENIED_ENV,
    MODE_FIRE,
    MODE_REVIEW,
    PHASE_COMMIT,
    PHASE_DRAFT,
    Hook,
    HookError,
    child_env,
    payload,
    project_json,
    run_hook,
    select_projects,
)
from projection.models import Project, ProjectFields, people


def _proj(title, **fields):
    return Project(fields=ProjectFields(title=title, **fields))


def _hook(command, **kw):
    return Hook(id=kw.pop("id", "test"), command=tuple(command), **kw)


def _script(tmp_path: Path, body: str, name: str = "hook.sh") -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# ==================== Which projects a hook receives ====================


def test_all_is_everything():
    projects = [_proj("a"), _proj("b")]
    assert select_projects(_hook(["x"]), projects=projects) == projects


def test_starred_filters():
    starred, plain = _proj("s", starred=True), _proj("p")
    chosen = select_projects(
        _hook(["x"], input="starred"), projects=[starred, plain]
    )
    assert [p.title for p in chosen] == ["s"]


def test_conflicts_filters():
    from projection.models import FieldConflict

    conflicted = _proj("c")
    conflicted.conflicts["note"] = FieldConflict(backend="b")
    chosen = select_projects(
        _hook(["x"], input="conflicts"), projects=[conflicted, _proj("quiet")]
    )
    assert [p.title for p in chosen] == ["c"]


def test_selection_uses_the_highlighted_project():
    picked = _proj("picked")
    chosen = select_projects(
        _hook(["x"], input="selection"),
        projects=[_proj("other"), picked],
        selected=picked,
    )
    assert [p.title for p in chosen] == ["picked"]


def test_selection_with_nothing_selected_is_empty():
    assert select_projects(
        _hook(["x"], input="selection"), projects=[_proj("a")], selected=None
    ) == []


# ==================== The payload ====================


def test_the_payload_uses_canonical_field_names():
    project = _proj(
        "ZTNA",
        status="In progress",
        note="a note",
        starred=True,
        due_date="2026-08-20",
        assigned=people([{"name": "Al", "email": "al@x.edu"}]),
    )
    body = json.loads(payload(_hook(["x"]), phase=PHASE_DRAFT, projects=[project]))

    assert body["hook"] == "test"
    assert body["phase"] == PHASE_DRAFT
    assert body["text"] is None
    item = body["projects"][0]
    assert item["title"] == "ZTNA"
    assert item["note"] == "a note"
    assert item["starred"] is True
    assert item["due_date"] == "2026-08-20"
    assert item["assigned"] == [{"name": "Al", "email": "al@x.edu"}]
    assert item["id"] == project.id


def test_the_payload_omits_projections_own_bookkeeping():
    """A small, stable shape is easier to write a script against."""
    project = _proj("ZTNA")
    project.link_remote("smartsheet", 1)
    project.set_base("smartsheet")
    project.touch("title")
    item = project_json(project)

    for internal in ("remote", "updated_at", "conflicts", "dirty", "fields"):
        assert internal not in item


def test_the_commit_payload_carries_the_approved_text():
    body = json.loads(
        payload(_hook(["x"]), phase=PHASE_COMMIT, projects=[], text="approved")
    )
    assert body["phase"] == PHASE_COMMIT
    assert body["text"] == "approved"


# ==================== Containment ====================


def test_the_environment_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("SOME_AMBIENT_SECRET", "nope")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env = child_env(_hook(["x"]))
    assert "SOME_AMBIENT_SECRET" not in env
    assert "PATH" in env


def test_a_hook_can_opt_into_a_variable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "needed-by-the-cli")
    assert "ANTHROPIC_API_KEY" not in child_env(_hook(["x"]))
    opted_in = _hook(["x"], env=("ANTHROPIC_API_KEY",))
    assert child_env(opted_in)["ANTHROPIC_API_KEY"] == "needed-by-the-cli"


def test_projections_own_credential_is_never_forwarded(monkeypatch):
    """Even when a hook asks for it by name: that is a mistake, not a request."""
    monkeypatch.setenv("SMARTSHEET_API_KEY", "must-not-propagate")
    asking = _hook(["x"], env=("SMARTSHEET_API_KEY",))
    assert "SMARTSHEET_API_KEY" not in child_env(asking)
    assert "SMARTSHEET_API_KEY" in DENIED_ENV


async def test_the_token_is_absent_from_the_real_child(tmp_path, monkeypatch):
    """End-to-end, not just the env builder."""
    script = _script(tmp_path, 'env > "$(dirname "$0")/env.txt"; echo ok')
    monkeypatch.setenv("SMARTSHEET_API_KEY", "must-not-propagate")
    await run_hook(
        _hook([script], env=("SMARTSHEET_API_KEY",)),
        phase=PHASE_DRAFT,
        projects=[],
    )
    assert "must-not-propagate" not in (tmp_path / "env.txt").read_text()


async def test_the_payload_goes_on_stdin_not_argv(tmp_path):
    """Keeps project text out of `ps` output and clear of ARG_MAX."""
    script = _script(
        tmp_path,
        'printf "%s\\n" "$@" > "$(dirname "$0")/args.txt";'
        ' cat > "$(dirname "$0")/stdin.txt"; echo ok',
    )
    await run_hook(
        _hook([script]), phase=PHASE_DRAFT, projects=[_proj("SECRET_TITLE")]
    )
    assert "SECRET_TITLE" not in (tmp_path / "args.txt").read_text()
    assert "SECRET_TITLE" in (tmp_path / "stdin.txt").read_text()


async def test_the_phase_is_passed_as_a_flag(tmp_path):
    script = _script(tmp_path, 'printf "%s\\n" "$@" > "$(dirname "$0")/args.txt"; echo ok')
    await run_hook(_hook([script]), phase=PHASE_COMMIT, projects=[])
    assert "--phase=commit" in (tmp_path / "args.txt").read_text()


async def test_extra_command_arguments_are_kept(tmp_path):
    script = _script(tmp_path, 'printf "%s\\n" "$@" > "$(dirname "$0")/args.txt"; echo ok')
    await run_hook(
        _hook([script, "--verbose", "x y"]), phase=PHASE_DRAFT, projects=[]
    )
    args = (tmp_path / "args.txt").read_text().splitlines()
    # "x y" stays one argument: nothing is shell-split.
    assert args[:3] == ["--verbose", "x y", "--phase=draft"]


async def test_the_hook_runs_outside_any_repo(tmp_path):
    """A repo cwd would pull in its .claude/settings.local.json allowlist."""
    script = _script(tmp_path, 'pwd > "$(dirname "$0")/cwd.txt"; echo ok')
    await run_hook(_hook([script]), phase=PHASE_DRAFT, projects=[])
    used = (tmp_path / "cwd.txt").read_text().strip()
    assert "projection-hook-" in used


# ==================== Failure modes ====================


async def test_stdout_comes_back(tmp_path):
    script = _script(tmp_path, 'echo "the draft"')
    out = await run_hook(_hook([script]), phase=PHASE_DRAFT, projects=[])
    assert out.strip() == "the draft"


async def test_a_nonzero_exit_raises_with_stderr(tmp_path):
    script = _script(tmp_path, 'echo "it went wrong" 1>&2; exit 3')
    with pytest.raises(HookError, match="it went wrong"):
        await run_hook(_hook([script]), phase=PHASE_DRAFT, projects=[])


async def test_a_missing_command_names_the_hook(tmp_path):
    with pytest.raises(HookError, match="config.toml"):
        await run_hook(
            _hook(["/nonexistent/hook-xyz"]), phase=PHASE_DRAFT, projects=[]
        )


async def test_a_hook_that_hangs_is_killed(tmp_path):
    """And killed *promptly* — the whole point is that the UI stops waiting.

    The duration bound is the load-bearing assertion. A grandchild inherits the
    pipes, so killing only the direct child leaves `proc.wait()` blocking until
    that grandchild exits: `HookError` is still raised, 30 seconds late. Without
    a bound here that bug passes the test.
    """
    script = _script(tmp_path, "sleep 30")
    started = time.monotonic()
    with pytest.raises(HookError, match="timed out"):
        await run_hook(
            _hook([script], timeout=0.3), phase=PHASE_DRAFT, projects=[]
        )
    assert time.monotonic() - started < 5, "the timeout did not take effect"


async def test_a_hook_that_backgrounds_something_still_times_out(tmp_path):
    """The exact shape of the bug: the child exits, a grandchild lives on."""
    script = _script(tmp_path, "sleep 30 &\nwait")
    started = time.monotonic()
    with pytest.raises(HookError, match="timed out"):
        await run_hook(
            _hook([script], timeout=0.3), phase=PHASE_DRAFT, projects=[]
        )
    assert time.monotonic() - started < 5


# ==================== Shape ====================


def test_display_prefers_the_label():
    assert _hook(["x"], label="Executive summary").display == "Executive summary"
    assert Hook(id="bare", command=("x",)).display == "bare"


def test_wants_review_only_in_review_mode():
    assert _hook(["x"], mode=MODE_REVIEW).wants_review is True
    assert _hook(["x"], mode=MODE_FIRE).wants_review is False


def test_the_action_string_carries_the_hook_id():
    assert _hook(["x"], id="exec-summary").action == "run_hook('exec-summary')"
