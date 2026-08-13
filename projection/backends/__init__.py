"""Backends: places Projection can mirror its local store to.

Exactly one backend is active at a time, and **none** is a valid choice — the
local store is the source of record, so Projection is fully usable with no
integration configured. `build_backend` returns None for that case.

The data model does not share that restriction: `Project.remote` and the merge
bases are keyed per backend, so adding a second one later is a config change
rather than a data migration. What is deliberately not taken on yet is *two live
at once*, which raises "which backend owns this field" with no good answer.
"""

from __future__ import annotations

from typing import Optional

from .. import secrets
from ..config import Config
from ..smartsheet_api import SmartsheetClient
from .base import (
    DEFAULT_COLUMNS,
    Backend,
    BackendError,
    Capabilities,
    FieldMap,
    ProbeResult,
    ProvisionResult,
    RecordRef,
    StaleRecordError,
)
from .d1 import NAME as D1, D1Backend
from .smartsheet import NAME as SMARTSHEET, SmartsheetBackend

__all__ = [
    "Backend",
    "BackendError",
    "Capabilities",
    "D1",
    "D1Backend",
    "DEFAULT_COLUMNS",
    "FieldMap",
    "ProbeResult",
    "ProvisionResult",
    "RecordRef",
    "smartsheet_client",
    "SMARTSHEET",
    "SmartsheetBackend",
    "StaleRecordError",
    "build_backend",
    "known_backends",
]


def smartsheet_client(config: Config) -> SmartsheetClient:
    """A Smartsheet client carrying the credential *config* names.

    The one way to build one. A bare `SmartsheetClient()` reads
    `secrets.SMARTSHEET`, which carries no reference — so it can only ever fall
    back to the environment variable, whatever config.toml says. That is precisely
    the bug that shipped when the built-in reference was removed: the app and the
    panel each pre-built a bare client and handed it to `build_backend`, so the
    credentialed branch below never ran for them.
    """
    return SmartsheetClient(
        credential=secrets.SMARTSHEET.with_ref(config.smartsheet.token_ref)
    )


def known_backends() -> tuple[str, ...]:
    """Every backend name this build can construct."""
    return (SMARTSHEET, D1)


def build_backend(
    config: Config,
    *,
    client: Optional[SmartsheetClient] = None,
    allow_unprovisioned: bool = False,
) -> Optional[Backend]:
    """The configured backend, or None when running with no integration.

    Args:
        config: the loaded configuration.
        client: an existing Smartsheet client to share. The backend does not
            close a client it was handed — whoever made it owns it.
        allow_unprovisioned: build the backend even with no target chosen yet.
            Only setup passes this: the resulting backend can `provision()` but
            not read or write, so it must never be handed to the coordinator.
            Everywhere else a missing target is an error, because the fallback
            would be reading whichever sheet a built-in default named.
    """
    name = (config.backend or "").strip().lower()
    if not name or name == "none":
        return None

    if name == SMARTSHEET:
        settings = config.smartsheet
        if not settings.projects_sheet_id and not allow_unprovisioned:
            # Loud, rather than falling back to a built-in id and reading a sheet
            # the user never asked for.
            raise BackendError(
                "backend = \"smartsheet\" but no sheet is configured. Set "
                "sheet_id under [backends.smartsheet] in config.toml, or run "
                "setup to create one."
            )
        return SmartsheetBackend(
            client or smartsheet_client(config),
            sheet_id=settings.projects_sheet_id,
            sheet_name=settings.projects_sheet_name,
            field_map=FieldMap(dict(settings.columns)),
            status_options=config.status_options,
        )

    if name == D1:
        settings = config.d1
        if not settings.account_id:
            # Nothing to guess: an account id is half of every D1 URL.
            raise BackendError(
                "backend = \"d1\" but no Cloudflare account is configured. Set "
                "account_id under [backends.d1] in config.toml, or run setup."
            )
        if not settings.database_id and not allow_unprovisioned:
            raise BackendError(
                "backend = \"d1\" but no database is configured. Set "
                "database_id under [backends.d1] in config.toml, or run setup "
                "to create one."
            )
        return D1Backend(
            account_id=settings.account_id,
            database_id=settings.database_id,
            database_name=settings.database_name,
            table=settings.table,
            credential=secrets.D1.with_ref(settings.token_ref),
        )

    raise BackendError(
        f"Unknown backend {name!r}. Known backends: "
        f"{', '.join(known_backends())} (or leave it empty for local-only)."
    )
