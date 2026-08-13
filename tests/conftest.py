"""Shared test setup."""

import sys
from pathlib import Path

import pytest

# `examples/exec_summary/` holds the worked hook example: `headless.py` and
# `summary_store.py` moved there when the exec summary stopped being built in.
# They still ship in this repo and still have tests, so they need to be
# importable — the script itself does the same insert for its own siblings.
_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "exec_summary"
if str(_EXAMPLE) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE))

from projection import config as config_module


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """Keep every test off the user's real config and data directories.

    Components fall back to `Config.load()` when they are not handed a config —
    which is exactly what happens when another app embeds the panel. Without
    this, a `profile = "vim"` in the developer's own config.toml would quietly
    change what the suite tests, running the suite would create a config file
    as a side effect, and `LocalStorage` would write into the real cache.
    """
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(
        config_module, "DEFAULT_CONFIG_FILE", tmp_path / "config" / "config.toml"
    )
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
