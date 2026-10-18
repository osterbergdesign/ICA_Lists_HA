"""Diagnostics for ICA Lists (no personnummer, password or tokens)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_PERSONNUMMER
from .coordinator import IcaConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_PERSONNUMMER}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IcaConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    token = runtime.auth.token
    coordinator = runtime.coordinator
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "login": {
            "has_token": token is not None,
            "access_token_expires": (
                datetime.fromtimestamp(token.expires_at, UTC).isoformat() if token else None
            ),
        },
        "last_update_success": coordinator.last_update_success,
        "lists": [
            {
                "id": lst.id,
                "name": lst.name,
                "items": len(lst.rows),
                "open": sum(1 for row in lst.rows if not row.striked),
            }
            for lst in (coordinator.data or {}).values()
        ],
        "mirror": runtime.mirror.status() if runtime.mirror else None,
        "recent_events": runtime.debug.recent(),
    }
