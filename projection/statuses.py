"""Status presentation: colour, glyph, and sort order.

A leaf module that imports nothing, deliberately. `models` needs `status_rank`
for its display ordering, `config` needs the whole set, and `config` must also be
importable from `hooks` — which imports `models`. Keeping these here is what stops
that becoming a cycle.

None of it is configurable: there are no TOML keys behind a status's colour,
glyph, or position, so nothing here needs a `Config` in hand.
"""

# The status meaning "finished" — set by Space and Ctrl+D in the edit modal.
DONE_STATUS = "Done"

# Display colors for every status observed in the sheet. Hex values shared by
# the sidebar and project rows.
STATUS_COLORS = {
    "Not started": "#98989D",
    "In progress": "#FFD60A",
    "Done": "#30D158",
    "On Hold": "#64D2FF",
    "Blocked": "#FF453A",
}

# Sidebar / row glyph for each status (single-cell characters only).
STATUS_ICONS = {
    "Not started": "○",
    "In progress": "◐",
    "Done": "●",
    "On Hold": "◇",
    "Blocked": "⊘",
}


def status_color(status: str) -> str:
    """Hex color for a status, falling back to neutral gray."""
    return STATUS_COLORS.get(status, "#98989D")


def status_icon(status: str) -> str:
    """Glyph for a status, falling back to an empty circle."""
    return STATUS_ICONS.get(status, "○")


# Display sort order for statuses (lower sorts first). Unknown statuses sort
# just before Done.
STATUS_RANK = {
    "In progress": 0,
    "Not started": 1,
    "Blocked": 2,
    "On Hold": 3,
    "Done": 5,
}


def status_rank(status: str) -> int:
    """Sort rank for a status (lower = higher in the list)."""
    return STATUS_RANK.get(status, 4)
