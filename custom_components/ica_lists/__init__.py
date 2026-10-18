"""ICA Lists: ICA shopping lists in Home Assistant, and a mirror from any to-do list."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_MIRROR_IMPORT,
    CONF_MIRROR_LIST,
    CONF_MIRROR_SOURCE,
    CONF_PERSONNUMMER,
    CONF_REMEMBER,
    DOMAIN,
    SERVICE_SYNC_NOW,
    STORAGE_VERSION,
    auth_store_key,
    mirror_store_key,
)
from .coordinator import IcaConfigEntry, IcaCoordinator, IcaRuntime
from .debuglog import async_get_debug_log
from .ica_api import IcaAuth, IcaClient, IcaToken
from .mirror import IcaMirror

PLATFORMS = [Platform.TODO]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the sync_now action."""

    async def _sync_now(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            runtime: IcaRuntime = entry.runtime_data
            await runtime.coordinator.async_refresh()
            if runtime.mirror is not None:
                await runtime.mirror.async_sync()

    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, _sync_now)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IcaConfigEntry) -> bool:
    """Sign in with the saved tokens and start the lists and the mirror."""
    debug = await async_get_debug_log(hass)
    store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, auth_store_key(str(entry.unique_id)), private=True
    )
    token = IcaToken.from_dict(await store.async_load())
    password = entry.data.get(CONF_PASSWORD) if entry.data.get(CONF_REMEMBER) else None
    credentials = (entry.data[CONF_PERSONNUMMER], password) if password else None
    if token is None and credentials is None:
        raise ConfigEntryAuthFailed("No saved ICA login")

    async def _save_token(new_token: IcaToken) -> None:
        await store.async_save(new_token.as_dict())

    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    auth = IcaAuth(
        session,
        token=token,
        on_token=_save_token,
        on_event=debug.add,
        credentials=credentials,
    )
    client = IcaClient(session, auth)
    coordinator = IcaCoordinator(hass, entry, client, debug)
    await coordinator.async_config_entry_first_refresh()

    mirror: IcaMirror | None = None
    source = entry.options.get(CONF_MIRROR_SOURCE)
    list_id = entry.options.get(CONF_MIRROR_LIST)
    if source and list_id:
        mirror = IcaMirror(
            hass,
            entry,
            coordinator,
            debug,
            source,
            list_id,
            bool(entry.options.get(CONF_MIRROR_IMPORT)),
        )
    entry.runtime_data = IcaRuntime(
        auth=auth, client=client, coordinator=coordinator, debug=debug, mirror=mirror
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if mirror is not None:
        await mirror.async_start()
        entry.async_on_unload(mirror.async_stop)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IcaConfigEntry) -> bool:
    """Unload the to-do lists (the mirror stops through async_on_unload)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: IcaConfigEntry) -> None:
    """Delete the saved tokens and mirror state when the integration is removed."""
    for key in (auth_store_key(str(entry.unique_id)), mirror_store_key(entry.entry_id)):
        await Store(hass, STORAGE_VERSION, key).async_remove()
