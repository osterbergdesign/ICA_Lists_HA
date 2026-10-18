"""Constants for ICA Lists."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ica_lists"

CONF_PERSONNUMMER: Final = "personnummer"
CONF_REMEMBER: Final = "remember_password"
CONF_LISTS: Final = "lists"
CONF_MIRROR_SOURCE: Final = "mirror_source"
CONF_MIRROR_LIST: Final = "mirror_list"
CONF_MIRROR_IMPORT: Final = "mirror_import"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_SCAN_INTERVAL: Final = 2  # minutes between checks of the ICA lists
MIRROR_POLL: Final = 60  # seconds between checks of the Home Assistant list
MIRROR_DEBOUNCE: Final = 3  # seconds to wait for more changes before syncing
MASS_DELETE_HOLD: Final = 180  # seconds a big removal must persist before it's mirrored
IMPORT_TIMEOUT: Final = 600  # seconds to wait for an imported item to show up
IMPORT_ATTEMPTS: Final = 3

STORAGE_VERSION: Final = 1
DEBUG_STORE_KEY: Final = f"{DOMAIN}.debug"

ISSUE_GEO_BLOCKED: Final = "geo_blocked"
ISSUE_MIRROR_LIST_MISSING: Final = "mirror_list_missing"

SERVICE_SYNC_NOW: Final = "sync_now"


def auth_store_key(account: str) -> str:
    """Storage key for an account's tokens (written before the entry exists)."""
    return f"{DOMAIN}.{account}.auth"


def mirror_store_key(entry_id: str) -> str:
    """Storage key for an entry's mirror state."""
    return f"{DOMAIN}.{entry_id}.mirror"
