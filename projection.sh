#!/usr/bin/env bash
# Launch the Projection TUI
cd "$(dirname "$0")" && exec uv run python run_tui.py "$@"
