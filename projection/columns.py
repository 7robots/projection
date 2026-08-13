"""Canonical field -> column-title maps.

A leaf module that imports nothing, on purpose. Both `config` (which reads the
mapping out of config.toml) and `backends` (which applies it) need these, and
`config` must not import `backends` — a backend is free to depend on
configuration, never the other way round.
"""

# What a target Projection creates itself gets. A backend it provisioned needs no
# mapping at all; the mapping in config.toml exists for *adopting* a target that
# already has its own vocabulary.
CANONICAL: dict[str, str] = {
    "title": "Title",
    "status": "Status",
    "assigned": "Assigned",
    "due_date": "Due Date",
    "note": "Note",
    "starred": "Starred",
}

# The Team Projects sheet's own column names.
#
# Used as the default map when the legacy `[smartsheet]` config section is in
# play instead of `[backends.smartsheet]`. That section only ever described this
# one sheet, so inferring its vocabulary keeps existing installs working without
# anyone hand-writing a mapping — and without the canonical defaults silently
# pointing at columns that sheet does not have.
SMARTSHEET_LEGACY: dict[str, str] = {
    "title": "Project",
    "status": "Status",
    "assigned": "Assigned To",
    "due_date": "Due Date",
    "note": "Update",
    "starred": "Sync",
}
