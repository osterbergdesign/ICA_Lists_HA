"""Keeps the ICA lists fresh for the to-do entities and the mirror."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, ISSUE_GEO_BLOCKED
from .ica_api import (
    IcaAuth,
    IcaAuthError,
    IcaClient,
    IcaError,
    IcaGeoBlockedError,
    IcaList,
    IcaRow,
)

if TYPE_CHECKING:
    from .debuglog import DebugLog
    from .mirror import IcaMirror

_LOGGER = logging.getLogger(__name__)


@dataclass
class IcaRuntime:
    """What a loaded entry keeps around."""

    auth: IcaAuth
    client: IcaClient
    coordinator: IcaCoordinator
    debug: DebugLog
    mirror: IcaMirror | None = None


type IcaConfigEntry = ConfigEntry[IcaRuntime]


class IcaCoordinator(DataUpdateCoordinator[dict[str, IcaList]]):
    """Fetches all ICA lists (with their items) every few minutes."""

    config_entry: IcaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IcaConfigEntry,
        client: IcaClient,
        debug: DebugLog,
    ) -> None:
        minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=max(1, int(minutes))),
            always_update=False,
        )
        self.client = client
        self.debug = debug

    async def _async_update_data(self) -> dict[str, IcaList]:
        try:
            lists = await self.client.async_get_lists()
        except IcaAuthError as err:
            self.debug.add("auth", f"sign-in needed: {err}")
            raise ConfigEntryAuthFailed(str(err)) from err
        except IcaGeoBlockedError as err:
            self.debug.add("api", str(err))
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_GEO_BLOCKED,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_GEO_BLOCKED,
            )
            raise UpdateFailed(str(err)) from err
        except IcaError as err:
            self.debug.add("api", f"couldn't read the lists: {err}")
            raise UpdateFailed(str(err)) from err
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_GEO_BLOCKED)
        return {lst.id: lst for lst in lists}

    # Writes update the local copy right away so the UI and the mirror don't
    # have to wait for the next poll.
    @callback
    def apply_row(self, list_id: str, row: IcaRow) -> None:
        lst = (self.data or {}).get(list_id)
        if lst is None:
            return
        for index, existing in enumerate(lst.rows):
            if existing.id == row.id:
                lst.rows[index] = row
                break
        else:
            lst.rows.append(row)
        self.async_update_listeners()

    @callback
    def remove_row(self, list_id: str, row_id: str) -> None:
        lst = (self.data or {}).get(list_id)
        if lst is None:
            return
        lst.rows = [row for row in lst.rows if row.id != row_id]
        self.async_update_listeners()
