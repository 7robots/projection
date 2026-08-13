"""Tests for reading config.toml into a Config object."""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

from projection import config as config_module
from projection.columns import CANONICAL, SMARTSHEET_LEGACY
from projection.config import (
    DEFAULT_STATUS_OPTIONS,
    Config,
    SyncConfig,
    KeysConfig,
    SmartsheetConfig,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


# ==================== The file itself ====================


def test_missing_file_is_created_with_the_documented_defaults(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    config = Config.load(path)

    assert path.exists()
    assert "backend" in path.read_text()
    # What was just written is the defaults, so the object matches them.
    assert config.smartsheet == SmartsheetConfig()
    assert config.load_error is None


def test_create_missing_false_writes_nothing(tmp_path):
    path = tmp_path / "config.toml"
    config = Config.load(path, create_missing=False)

    assert not path.exists()
    assert config.smartsheet == SmartsheetConfig()


def test_values_are_read_from_the_file(tmp_path):
    path = _write(
        tmp_path,
        """
        [smartsheet]
        projects_sheet_id = 111
        projects_sheet_name = "My Work"

        [sync]
        poll_interval = 300
        """,
    )
    config = Config.load(path)

    assert config.smartsheet.projects_sheet_id == 111
    assert config.smartsheet.projects_sheet_name == "My Work"
    assert config.sync == SyncConfig(poll_interval=300.0)
    assert config.load_error is None


def test_the_poll_interval_is_still_read_from_the_old_section(tmp_path):
    """It lived in [headless], which never described it. Old files keep working."""
    path = _write(tmp_path, "[headless]\npoll_interval = 60\n")
    assert Config.load(path).sync.poll_interval == 60.0


def test_the_new_section_wins_for_the_poll_interval(tmp_path):
    path = _write(
        tmp_path, "[headless]\npoll_interval = 60\n\n[sync]\npoll_interval = 5\n"
    )
    assert Config.load(path).sync.poll_interval == 5.0


# ==================== Bad values are reported, not swallowed ====================


def test_a_non_numeric_sheet_id_falls_back_and_says_so(tmp_path):
    """Silently using the built-in id means reading someone else's sheet."""
    path = _write(
        tmp_path,
        '[smartsheet]\nprojects_sheet_id = "not-a-number"\n',
    )
    config = Config.load(path)

    assert config.smartsheet.projects_sheet_id == SmartsheetConfig().projects_sheet_id
    assert config.load_error is not None
    assert "projects_sheet_id" in config.load_error


def test_a_boolean_sheet_id_is_rejected(tmp_path):
    """bool is an int subclass, so int(True) == 1 would sail through."""
    path = _write(tmp_path, "[smartsheet]\nprojects_sheet_id = true\n")
    config = Config.load(path)

    assert config.smartsheet.projects_sheet_id == SmartsheetConfig().projects_sheet_id
    assert "projects_sheet_id" in (config.load_error or "")


def test_unparseable_toml_falls_back_and_says_so(tmp_path):
    path = _write(tmp_path, "[smartsheet\nthis is not toml")
    config = Config.load(path)

    assert config.smartsheet == SmartsheetConfig()
    assert config.load_error is not None
    assert str(path) in config.load_error


def test_every_bad_value_is_reported_not_just_the_first(tmp_path):
    path = _write(
        tmp_path,
        """
        [smartsheet]
        projects_sheet_id = "nope"

        [status_options]
        projects = "also nope"
        """,
    )
    config = Config.load(path)

    assert "projects_sheet_id" in (config.load_error or "")
    assert "status_options" in (config.load_error or "")


def test_a_wrong_typed_section_is_ignored(tmp_path):
    """`smartsheet = 5` is not a table; reading keys from it must not crash."""
    path = _write(tmp_path, "smartsheet = 5\n")
    config = Config.load(path)

    assert config.smartsheet == SmartsheetConfig()


# ==================== Status options ====================


def test_configured_status_order_wins(tmp_path):
    path = _write(
        tmp_path,
        '[status_options]\nprojects = ["Done", "In progress"]\n',
    )
    config = Config.load(path)

    assert config.status_options[0] == "Done"
    assert config.status_options[1] == "In progress"


def test_statuses_missing_from_a_stale_config_are_appended(tmp_path):
    """A config predating a new status must not hide it."""
    path = _write(tmp_path, '[status_options]\nprojects = ["In progress"]\n')
    config = Config.load(path)

    assert set(DEFAULT_STATUS_OPTIONS) <= set(config.status_options)
    assert config.status_options[0] == "In progress"


def test_statuses_of_the_wrong_shape_fall_back(tmp_path):
    path = _write(tmp_path, '[status_options]\nprojects = "In progress"\n')
    config = Config.load(path)

    assert config.status_options == DEFAULT_STATUS_OPTIONS
    assert "status_options" in (config.load_error or "")


# ==================== Keys ====================


def test_key_profile_and_overrides_are_split(tmp_path):
    path = _write(
        tmp_path,
        """
        [keys]
        profile = "vim"
        "project.new" = "w"
        "view.refresh" = "F5"
        """,
    )
    config = Config.load(path)

    assert config.keys.profile == "vim"
    assert config.keys.vim is True
    # `profile` is a setting, not a binding id.
    assert config.keys.overrides == {"project.new": "w", "view.refresh": "F5"}


def test_non_string_overrides_are_skipped(tmp_path):
    path = _write(tmp_path, '[keys]\n"project.new" = 7\n')
    config = Config.load(path)

    assert config.keys.overrides == {}


def test_default_profile_is_not_vim():
    assert Config().keys.vim is False
    assert KeysConfig().profile == "default"


# ==================== data_dir ====================


def test_data_dir_follows_the_module_default(monkeypatch, tmp_path):
    """A default_factory, so redirecting DATA_DIR actually takes effect."""
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "elsewhere")
    assert Config().data_dir == tmp_path / "elsewhere"


# ==================== The phase-0 invariant ====================

# Names that used to be module-level constants evaluated at import. They are
# gone on purpose: a setup wizard has to be able to write config.toml and
# reload it, and per-backend settings need somewhere to live that is not a flat
# namespace of globals. Re-adding one would reintroduce both problems.
_REMOVED_GLOBALS = (
    "PROJECTS_SHEET_ID",
    "PROJECTS_SHEET_NAME",
    "CLAUDE_BIN",
    "SUMMARY_MODEL",
    "HEADLESS_TIMEOUT",
    "DATA_POLL_INTERVAL",
    "KEY_PROFILE",
    "KEY_OVERRIDES",
    "STATUS_OPTIONS",
)


def test_no_config_values_are_module_globals():
    for name in _REMOVED_GLOBALS:
        assert not hasattr(config_module, name), (
            f"config.{name} is back as a module global — it should be a field "
            "on Config, read via Config.load()"
        )


def test_status_presentation_stays_importable_without_a_config():
    """Colors, glyphs and sort rank have no TOML keys, so they stay globals."""
    assert config_module.status_color("Done").startswith("#")
    assert len(config_module.status_icon("Blocked")) == 1
    assert config_module.status_rank("In progress") < config_module.status_rank("Done")
    assert config_module.DONE_STATUS == "Done"


def test_importing_the_package_reads_and_writes_no_config_file(tmp_path):
    """The point of the refactor: import is pure, `load()` touches disk.

    Run in a subprocess with HOME redirected, because the check is about what
    happens at import — which has already happened in this process.
    """
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "PYTHONPATH": str(repo_root),
    }
    result = subprocess.run(
        [sys.executable, "-c", "import projection.config, projection.panel"],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )

    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.rglob("*.toml")), "importing wrote a config file"


# ==================== Backend selection and column mapping ====================


def test_the_modern_backend_section_is_read(tmp_path):
    path = _write(
        tmp_path,
        """
        backend = "smartsheet"

        [backends.smartsheet]
        sheet_id = 555
        sheet_name = "Team Projects"
        """,
    )
    config = Config.load(path)
    assert config.backend == "smartsheet"
    assert config.smartsheet.projects_sheet_id == 555
    assert config.smartsheet.projects_sheet_name == "Team Projects"


def test_the_modern_section_defaults_to_canonical_column_names(tmp_path):
    """A sheet Projection provisions itself needs no mapping at all."""
    path = _write(tmp_path, '[backends.smartsheet]\nsheet_id = 555\n')
    assert Config.load(path).smartsheet.columns == CANONICAL


def test_the_legacy_section_implies_the_legacy_column_names(tmp_path):
    """That section only ever described Team Projects, so infer its names."""
    path = _write(tmp_path, "[smartsheet]\nprojects_sheet_id = 555\n")
    assert Config.load(path).smartsheet.columns == SMARTSHEET_LEGACY


def test_a_column_mapping_overrides_field_by_field(tmp_path):
    """Adopting a sheet means renaming some columns, not restating all of them."""
    path = _write(
        tmp_path,
        """
        [backends.smartsheet]
        sheet_id = 555

        [backends.smartsheet.columns]
        title = "Project"
        note = "Update"
        """,
    )
    columns = Config.load(path).smartsheet.columns
    assert columns["title"] == "Project"
    assert columns["note"] == "Update"
    # Everything unmentioned keeps its canonical name.
    assert columns["status"] == CANONICAL["status"]


def test_the_modern_section_wins_over_the_legacy_one(tmp_path):
    path = _write(
        tmp_path,
        """
        [smartsheet]
        projects_sheet_id = 111

        [backends.smartsheet]
        sheet_id = 222
        """,
    )
    assert Config.load(path).smartsheet.projects_sheet_id == 222


def test_the_modern_section_is_read_without_a_legacy_one(tmp_path):
    path = _write(
        tmp_path,
        """
        [backends.smartsheet]
        sheet_id = 222
        """,
    )
    assert Config.load(path).smartsheet.projects_sheet_id == 222


def test_a_column_mapped_to_a_non_field_is_reported(tmp_path):
    path = _write(
        tmp_path,
        '[backends.smartsheet.columns]\nnonsense = "Whatever"\n',
    )
    config = Config.load(path)
    assert "nonsense" in (config.load_error or "")
    assert "nonsense" not in config.smartsheet.columns


def test_a_non_text_column_title_is_reported(tmp_path):
    path = _write(tmp_path, "[backends.smartsheet.columns]\ntitle = 7\n")
    config = Config.load(path)
    assert "title" in (config.load_error or "")
    # And the canonical name is kept rather than a number becoming a column.
    assert config.smartsheet.columns["title"] == CANONICAL["title"]


def test_a_non_text_backend_falls_back_to_local_only(tmp_path):
    path = _write(tmp_path, "backend = 42\n")
    config = Config.load(path)
    assert config.backend == ""
    assert "backend" in (config.load_error or "")


def test_the_backend_name_is_normalized(tmp_path):
    path = _write(tmp_path, 'backend = "  SmartSheet  "\n')
    assert Config.load(path).backend == "smartsheet"


def test_a_bad_modern_sheet_id_is_reported(tmp_path):
    path = _write(tmp_path, '[backends.smartsheet]\nsheet_id = "nope"\n')
    config = Config.load(path)
    assert "sheet_id" in (config.load_error or "")
    assert config.smartsheet.projects_sheet_id == SmartsheetConfig().projects_sheet_id


# ==================== Hooks ====================


def test_a_hook_is_read(tmp_path):
    path = _write(
        tmp_path,
        """
        [[hooks]]
        id = "exec-summary"
        label = "Executive summary"
        key = "x"
        command = ["/bin/echo", "hi"]
        input = "starred"
        mode = "review"
        timeout = 240
        env = ["ANTHROPIC_API_KEY"]
        review_title = "Review it"
        """,
    )
    hook = Config.load(path).hooks[0]
    assert hook.id == "exec-summary"
    assert hook.label == "Executive summary"
    assert hook.key == "x"
    assert hook.command == ("/bin/echo", "hi")
    assert hook.input == "starred"
    assert hook.mode == "review"
    assert hook.timeout == 240.0
    assert hook.env == ("ANTHROPIC_API_KEY",)
    assert hook.review_title == "Review it"


def test_no_hooks_by_default(tmp_path):
    assert Config.load(tmp_path / "config.toml").hooks == ()


def test_the_command_program_is_path_expanded(tmp_path):
    path = _write(
        tmp_path,
        '[[hooks]]\nid = "h"\ncommand = ["~/bin/script", "~/keep-me"]\n',
    )
    hook = Config.load(path).hooks[0]
    assert hook.command[0].startswith(str(Path.home()))
    # Arguments are not expanded: only the program is a path by definition.
    assert hook.command[1] == "~/keep-me"


def test_a_command_given_as_one_string_is_refused(tmp_path):
    """Splitting it would be shell parsing, which is exactly what we avoid."""
    path = _write(tmp_path, '[[hooks]]\nid = "h"\ncommand = "script --flag"\n')
    config = Config.load(path)
    assert config.hooks == ()
    assert "list of arguments" in (config.load_error or "")


def test_a_hook_without_an_id_is_skipped(tmp_path):
    path = _write(tmp_path, '[[hooks]]\ncommand = ["/bin/echo"]\n')
    config = Config.load(path)
    assert config.hooks == ()
    assert "id" in (config.load_error or "")


def test_a_hook_without_a_command_is_skipped(tmp_path):
    path = _write(tmp_path, '[[hooks]]\nid = "h"\n')
    config = Config.load(path)
    assert config.hooks == ()
    assert "command" in (config.load_error or "")


def test_a_duplicate_hook_id_is_skipped(tmp_path):
    path = _write(
        tmp_path,
        '[[hooks]]\nid = "h"\ncommand = ["/bin/echo"]\n'
        '[[hooks]]\nid = "h"\ncommand = ["/bin/true"]\n',
    )
    config = Config.load(path)
    assert len(config.hooks) == 1
    assert "repeats id" in (config.load_error or "")


def test_one_bad_hook_does_not_cost_you_the_good_one(tmp_path):
    path = _write(
        tmp_path,
        '[[hooks]]\nid = "bad"\n'
        '[[hooks]]\nid = "good"\ncommand = ["/bin/echo"]\n',
    )
    config = Config.load(path)
    assert [h.id for h in config.hooks] == ["good"]
    assert config.load_error is not None


def test_an_unknown_input_falls_back_to_all(tmp_path):
    path = _write(
        tmp_path,
        '[[hooks]]\nid = "h"\ncommand = ["/bin/echo"]\ninput = "everything"\n',
    )
    config = Config.load(path)
    assert config.hooks[0].input == "all"
    assert "input" in (config.load_error or "")


def test_an_unknown_mode_falls_back_to_fire(tmp_path):
    """Falling back to review would invent a confirmation step nobody asked for."""
    path = _write(
        tmp_path,
        '[[hooks]]\nid = "h"\ncommand = ["/bin/echo"]\nmode = "maybe"\n',
    )
    config = Config.load(path)
    assert config.hooks[0].mode == "fire"
    assert "mode" in (config.load_error or "")


def test_a_nonpositive_timeout_is_refused(tmp_path):
    path = _write(
        tmp_path, '[[hooks]]\nid = "h"\ncommand = ["/bin/echo"]\ntimeout = 0\n'
    )
    config = Config.load(path)
    assert config.hooks[0].timeout == 120.0
    assert "timeout" in (config.load_error or "")


def test_hooks_of_the_wrong_shape_are_ignored(tmp_path):
    path = _write(tmp_path, 'hooks = "not a list"\n')
    config = Config.load(path)
    assert config.hooks == ()
    assert "hooks" in (config.load_error or "")


def test_a_hook_needs_no_key(tmp_path):
    """It is still reachable from the command palette."""
    path = _write(tmp_path, '[[hooks]]\nid = "h"\ncommand = ["/bin/echo"]\n')
    assert Config.load(path).hooks[0].key == ""


# ==================== Writing it back ====================


def _round_trip(tmp_path: Path, config: Config) -> Config:
    """Save, then read back — the property setup depends on."""
    path = tmp_path / "config.toml"
    config.save(path)
    return Config.load(path)


def test_every_setting_survives_a_save(tmp_path):
    """Setup rewrites the whole file, so anything it drops is lost."""
    from projection.hooks import Hook

    config = Config(
        backend="smartsheet",
        smartsheet=SmartsheetConfig(
            projects_sheet_id=123456789,
            projects_sheet_name='EA "Current" Work',
            columns={**CANONICAL, "note": "Update", "title": "Project"},
        ),
        sync=SyncConfig(poll_interval=300),
        keys=KeysConfig(profile="vim", overrides={"project.new": "w"}),
        hooks=(
            Hook(
                id="ia-summary",
                command=("/usr/local/bin/summary", "--quiet"),
                label="Exec summary",
                key="x",
                input="starred",
                mode="review",
                timeout=240.0,
                env=("ANTHROPIC_API_KEY",),
                review_title="Review — summary",
            ),
        ),
        status_options=("Cooking", "Done"),
    )

    written = _round_trip(tmp_path, config)

    assert written.backend == config.backend
    assert written.smartsheet == config.smartsheet
    assert written.sync == config.sync
    assert written.keys == config.keys
    assert written.hooks == config.hooks
    # Statuses are the one setting a load *adds* to, by design: a file predating
    # a built-in status must not hide it. What matters is the user's order.
    assert written.status_options[:2] == ("Cooking", "Done")
    assert written.load_error is None


def test_a_second_save_is_stable(tmp_path):
    """Load-save-load-save must converge, or setup drifts the file each run."""
    first = _round_trip(tmp_path, Config(backend="smartsheet",
                                        smartsheet=SmartsheetConfig(projects_sheet_id=7)))
    path = tmp_path / "config.toml"
    text = path.read_text()
    first.save(path)
    assert path.read_text() == text


def test_the_previous_file_is_kept_as_a_backup(tmp_path):
    """The writer does not preserve hand-written comments, so it keeps the file."""
    path = _write(tmp_path, "# my own notes\nbackend = \"\"\n")
    Config.load(path).save(path)

    backup = tmp_path / "config.toml.bak"
    assert backup.exists()
    assert "my own notes" in backup.read_text()


def test_save_writes_back_to_where_it_was_loaded_from(tmp_path):
    """Otherwise setup in a test — or against an override — hits the real file."""
    path = tmp_path / "elsewhere.toml"
    path.write_text('backend = "smartsheet"\n[backends.smartsheet]\nsheet_id = 5\n')
    config = Config.load(path)
    assert config.source == path

    config.save()
    assert Config.load(path).smartsheet.projects_sheet_id == 5


def test_canonical_columns_are_not_restated(tmp_path):
    """A sheet Projection provisioned needs no mapping at all."""
    config = Config(
        backend="smartsheet", smartsheet=SmartsheetConfig(projects_sheet_id=1)
    )
    path = tmp_path / "config.toml"
    config.save(path)
    assert "[backends.smartsheet.columns]" not in path.read_text()
    assert Config.load(path).smartsheet.columns == CANONICAL


def test_a_quoted_sheet_name_does_not_break_the_file(tmp_path):
    config = Config(
        backend="smartsheet",
        smartsheet=SmartsheetConfig(
            projects_sheet_id=1, projects_sheet_name='He said "hi"\\done'
        ),
    )
    written = _round_trip(tmp_path, config)
    assert written.smartsheet.projects_sheet_name == 'He said "hi"\\done'
    assert written.load_error is None


def test_binding_overrides_stay_under_keys(tmp_path):
    """A dotted binding id unquoted would nest a table instead."""
    config = Config(keys=KeysConfig(overrides={"project.new": "w"}))
    path = tmp_path / "config.toml"
    config.save(path)
    assert '"project.new" = "w"' in path.read_text()
    assert Config.load(path).keys.overrides == {"project.new": "w"}


# ==================== First run ====================


def test_a_created_file_reports_a_first_run(tmp_path):
    assert Config.load(tmp_path / "config.toml").first_run is True


def test_an_existing_file_does_not(tmp_path):
    path = _write(tmp_path, 'backend = ""\n')
    assert Config.load(path).first_run is False


def test_an_unreadable_file_is_not_a_first_run(tmp_path):
    """It has content — offering to overwrite it is the wrong answer to a typo."""
    path = _write(tmp_path, "backend = [unclosed\n")
    config = Config.load(path)
    assert config.first_run is False
    assert config.load_error is not None


# ==================== Per-backend settings ====================


def test_the_d1_table_is_read(tmp_path):
    path = _write(
        tmp_path,
        'backend = "d1"\n[backends.d1]\n'
        'account_id = "acct-1"\ndatabase_id = "db-1"\n'
        'database_name = "projection"\ntable = "work"\n'
        'token_ref = "op://Private/cloudflare/token"\n',
    )
    config = Config.load(path)
    assert config.backend == "d1"
    assert config.d1.account_id == "acct-1"
    assert config.d1.database_id == "db-1"
    assert config.d1.table == "work"
    assert config.d1.token_ref == "op://Private/cloudflare/token"


def test_a_d1_table_name_falls_back_rather_than_being_empty(tmp_path):
    """Every write names this table; "" would be a syntax error at query time."""
    path = _write(tmp_path, '[backends.d1]\ntable = ""\n')
    assert Config.load(path).d1.table == "projects"


def test_a_non_text_d1_value_is_reported(tmp_path):
    path = _write(tmp_path, "[backends.d1]\naccount_id = 42\n")
    config = Config.load(path)
    assert config.d1.account_id == ""
    assert "account_id" in (config.load_error or "")


def test_d1_settings_survive_a_save(tmp_path):
    from projection.config import D1Config

    config = Config(
        backend="d1",
        d1=D1Config(
            account_id="acct-1",
            database_id="db-1",
            database_name="projection",
            table="work",
            token_ref="op://Private/cloudflare/token",
        ),
    )
    written = _round_trip(tmp_path, config)
    assert written.d1 == config.d1
    assert written.backend == "d1"


def test_switching_backends_keeps_the_other_ones_settings(tmp_path):
    """Turning one off, or swapping, must not mean re-entering ids."""
    from projection.config import D1Config

    config = Config(
        backend="smartsheet",
        smartsheet=SmartsheetConfig(projects_sheet_id=7),
        d1=D1Config(account_id="acct-1", database_id="db-1"),
    )
    switched = config.with_backend_values("d1", {"account_id": "acct-2"})
    # The one named changed; the other did not.
    assert switched.d1.account_id == "acct-2"
    assert switched.d1.database_id == "db-1"
    assert switched.smartsheet.projects_sheet_id == 7

    written = _round_trip(tmp_path, dataclasses.replace(switched, backend="d1"))
    assert written.smartsheet.projects_sheet_id == 7
    assert written.d1.account_id == "acct-2"


def test_backend_values_round_trip_through_a_form(tmp_path):
    """What the setup dialog reads is what `with_backend_values` accepts back."""
    config = Config(smartsheet=SmartsheetConfig(projects_sheet_id=7, projects_sheet_name="Mine"))
    values = config.backend_values("smartsheet")
    assert values["sheet_id"] == 7
    assert config.with_backend_values("smartsheet", values).smartsheet == config.smartsheet


def test_a_provisioned_target_lands_in_the_right_key():
    from projection.config import D1Config

    config = Config(d1=D1Config(account_id="acct-1"))
    assert config.with_provisioned_target("d1", "new-db", "projection").d1.database_id == "new-db"
    sheet = Config().with_provisioned_target("smartsheet", "777", "New sheet")
    assert sheet.smartsheet.projects_sheet_id == 777
    assert sheet.smartsheet.projects_sheet_name == "New sheet"


def test_the_backend_names_match_the_backends_that_define_them():
    """`config` cannot import a backend, so the names are written twice."""
    from projection.backends.d1 import NAME as D1_NAME
    from projection.backends.smartsheet import NAME as SMARTSHEET_NAME
    from projection.config import D1_BACKEND, SMARTSHEET_BACKEND

    assert (SMARTSHEET_BACKEND, D1_BACKEND) == (SMARTSHEET_NAME, D1_NAME)


def test_a_d1_table_has_nothing_to_map():
    assert Config().backend_columns("d1") == CANONICAL


def test_a_smartsheet_token_ref_is_read(tmp_path):
    """It was in the dataclass and the writer but not the reader for a while.

    The symptom was silent: a correct `token_ref` was ignored, which only became
    load-bearing once the package stopped carrying a default reference.
    """
    path = _write(
        tmp_path,
        '[backends.smartsheet]\nsheet_id = 1\n'
        'token_ref = "op://Private/sheets/token"\n',
    )
    assert Config.load(path).smartsheet.token_ref == "op://Private/sheets/token"


def test_a_token_ref_survives_a_save(tmp_path):
    config = Config(
        backend="smartsheet",
        smartsheet=SmartsheetConfig(
            projects_sheet_id=1, token_ref="op://Private/sheets/token"
        ),
    )
    assert _round_trip(tmp_path, config).smartsheet.token_ref == config.smartsheet.token_ref


def test_the_configured_ref_reaches_the_real_apps_client(tmp_path):
    """The path the *app* takes, which is not the one build_backend takes alone.

    `ProjectsApp` and `ProjectsPanel` each build a client and hand it to
    `build_backend`, which then uses it as-is — so testing `build_backend(config)`
    with no client exercises the wrong branch. That is exactly how removing the
    built-in reference shipped an app that could not find any credential.
    """
    from projection.app import ProjectsApp
    from projection.panel import ProjectsPanel

    config = Config(
        backend="smartsheet",
        smartsheet=SmartsheetConfig(
            projects_sheet_id=1, token_ref="op://Private/sheets/token"
        ),
    )

    for built in (
        ProjectsApp(config=config)._client,
        ProjectsPanel(config=config)._client,
        ProjectsPanel(config=config)._backend._client,
    ):
        assert built._credential.secret_ref == "op://Private/sheets/token"


def test_the_configured_ref_reaches_the_backends_client(tmp_path):
    """End of the chain: config.toml -> Credential -> the client that reads it."""
    from projection.backends import build_backend

    config = Config(
        backend="smartsheet",
        smartsheet=SmartsheetConfig(
            projects_sheet_id=1, token_ref="op://Private/sheets/token"
        ),
    )
    backend = build_backend(config)
    assert backend._client._credential.secret_ref == "op://Private/sheets/token"

    from projection.config import D1Config

    d1 = build_backend(
        Config(
            backend="d1",
            d1=D1Config(
                account_id="a", database_id="b", token_ref="op://Private/cf/token"
            ),
        )
    )
    assert d1._client._credential.secret_ref == "op://Private/cf/token"


def test_saving_writes_through_a_symlink_rather_than_over_it(tmp_path):
    """A config symlinked out of a dotfiles repo must keep syncing after a save.

    The write is `os.replace`, which replaces the link itself unless the path is
    resolved first — so the repo copy would go stale and every other machine
    would keep reading it, with nothing to suggest why.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "config.toml"
    tracked.write_text('backend = ""\n')

    home = tmp_path / "home"
    home.mkdir()
    link = home / "config.toml"
    link.symlink_to(tracked)

    config = Config.load(link)
    dataclasses.replace(
        config,
        backend="smartsheet",
        smartsheet=SmartsheetConfig(projects_sheet_id=42),
    ).save(link)

    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert "42" in tracked.read_text(), "the repo copy did not receive the change"
    assert Config.load(link).smartsheet.projects_sheet_id == 42


def test_the_backup_lands_beside_the_real_file(tmp_path):
    """Not beside the link: the .bak belongs with what it backs up."""
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "config.toml"
    tracked.write_text('backend = "smartsheet"\n')

    home = tmp_path / "home"
    home.mkdir()
    link = home / "config.toml"
    link.symlink_to(tracked)

    Config.load(link).save(link)

    assert (repo / "config.toml.bak").exists()
    assert not (home / "config.toml.bak").exists()
