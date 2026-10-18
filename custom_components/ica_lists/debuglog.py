"""A short log of recent ICA events, kept in .storage and in diagnostics.

Messages pass through `sanitize`, so personnummer, passwords and tokens never
end up here. Grocery item names do, since they help explain what synced.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DEBUG_STORE_KEY, STORAGE_VERSION
from .ica_api import sanitize

MAX_EVENTS = 80
DATA_DEBUG: HassKey[DebugLog] = HassKey("ica_lists_debug")


class DebugLog:
    """Ring buffer of events, saved a little after each change."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, DEBUG_STORE_KEY)
        self._events: deque[dict[str, str]] = deque(maxlen=MAX_EVENTS)

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        for event in data.get("events", [])[-MAX_EVENTS:]:
            if isinstance(event, dict):
                self._events.append(event)

    @callback
    def add(self, kind: str, message: str) -> None:
        self._events.append(
            {
                "t": dt_util.utcnow().isoformat(timespec="seconds"),
                "kind": kind,
                "msg": sanitize(message),
            }
        )
        self._store.async_delay_save(self._data, 15)

    def _data(self) -> dict[str, Any]:
        return {"events": list(self._events)}

    def recent(self, count: int = 40) -> list[dict[str, str]]:
        return list(self._events)[-count:]


async def async_get_debug_log(hass: HomeAssistant) -> DebugLog:
    """The shared debug log, loaded on first use."""
    if (log := hass.data.get(DATA_DEBUG)) is None:
        log = DebugLog(hass)
        hass.data[DATA_DEBUG] = log
        await log.async_load()
    return log
