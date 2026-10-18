"""Setup and options screens for ICA Lists."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.storage import Store

from .const import (
    CONF_LISTS,
    CONF_MIRROR_IMPORT,
    CONF_MIRROR_LIST,
    CONF_MIRROR_SOURCE,
    CONF_PERSONNUMMER,
    CONF_REMEMBER,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
    auth_store_key,
)
from .debuglog import async_get_debug_log
from .ica_api import (
    IcaApiError,
    IcaAuth,
    IcaAuthError,
    IcaClient,
    IcaConnectionError,
    IcaGeoBlockedError,
    IcaList,
    IcaLoginFlowError,
    IcaToken,
    account_id,
    mask_personnummer,
    normalize_personnummer,
    personnummer_valid,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
TODO_SELECTOR = EntitySelector(EntitySelectorConfig(filter={"domain": "todo"}))
SHOPPING_WORDS = ("shopping", "inkop", "handla", "grocer", "mat")


async def _async_login(
    hass: HomeAssistant, pnr: str, password: str
) -> tuple[IcaToken, str | None, list[IcaList]]:
    """Log in once and read the lists. Raises the client's errors."""
    debug = await async_get_debug_log(hass)
    session = async_create_clientsession(
        hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
    )
    try:
        auth = IcaAuth(session, on_event=debug.add)
        await auth.async_login(pnr, password)
        lists = await IcaClient(session, auth).async_get_lists()
        assert auth.token is not None
        return auth.token, auth.account_name, lists
    finally:
        session.detach()


def _error(err: Exception) -> tuple[str, str]:
    """Form error key and a detail line for the message."""
    if isinstance(err, IcaAuthError):
        return "invalid_auth", ""
    if isinstance(err, IcaGeoBlockedError):
        return "geo_blocked", ""
    if isinstance(err, IcaLoginFlowError):
        return "login_flow", f"{err.step}: {err.detail}"
    if isinstance(err, IcaApiError):
        return "api_error", str(err)
    if isinstance(err, IcaConnectionError):
        return "cannot_connect", str(err)
    _LOGGER.exception("Unexpected error while logging in to ICA")
    return "unknown", type(err).__name__


def _list_options(lists: list[IcaList], counts: bool = False) -> list[SelectOptionDict]:
    def label(lst: IcaList) -> str:
        if not counts:
            return lst.name
        open_items = sum(1 for row in lst.rows if not row.striked)
        return f"{lst.name} ({open_items})"

    return [SelectOptionDict(value=lst.id, label=label(lst)) for lst in lists]


def _default_list(lists: list[IcaList]) -> str | None:
    for lst in lists:
        if lst.name.strip().casefold() in ("handla", "inköpslista", "inkopslista"):
            return lst.id
    return lists[0].id if lists else None


def _own_entity(hass: HomeAssistant, entity_id: str) -> bool:
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == DOMAIN


def _suggest_source(hass: HomeAssistant) -> str | None:
    """A likely shopping list to mirror, e.g. todo.google_keep_shopping_list."""
    candidates = [
        state.entity_id
        for state in hass.states.async_all("todo")
        if not _own_entity(hass, state.entity_id)
    ]
    for words in (("google_keep",), ()):
        for entity_id in candidates:
            if all(w in entity_id for w in words) and any(
                w in entity_id for w in SHOPPING_WORDS
            ):
                return entity_id
    return None


def _check_source(hass: HomeAssistant, source: str | None) -> str | None:
    if not source:
        return None
    if _own_entity(hass, source):
        return "mirror_self"
    if hass.states.get(source) is None:
        return "source_not_found"
    return None


def _mirror_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {CONF_MIRROR_IMPORT: bool(user_input.get(CONF_MIRROR_IMPORT))}
    if user_input.get(CONF_MIRROR_SOURCE) and user_input.get(CONF_MIRROR_LIST):
        options[CONF_MIRROR_SOURCE] = user_input[CONF_MIRROR_SOURCE]
        options[CONF_MIRROR_LIST] = user_input[CONF_MIRROR_LIST]
    return options


class IcaListsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in, pick lists, pick what to mirror."""

    VERSION = 1

    def __init__(self) -> None:
        self._pnr = ""
        self._password = ""
        self._remember = True
        self._token: IcaToken | None = None
        self._name: str | None = None
        self._lists: list[IcaList] = []
        self._chosen: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IcaListsOptionsFlow:
        return IcaListsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            pnr = normalize_personnummer(user_input[CONF_PERSONNUMMER])
            if not personnummer_valid(pnr):
                errors[CONF_PERSONNUMMER] = "invalid_personnummer"
            else:
                await self.async_set_unique_id(account_id(pnr))
                self._abort_if_unique_id_configured()
                try:
                    self._token, self._name, self._lists = await _async_login(
                        self.hass, pnr, user_input[CONF_PASSWORD]
                    )
                except Exception as err:  # noqa: BLE001 - every error gets a message
                    errors["base"], detail = _error(err)
                else:
                    self._pnr = pnr
                    self._password = user_input[CONF_PASSWORD]
                    self._remember = bool(user_input.get(CONF_REMEMBER, True))
                    return await self.async_step_lists()

        schema = vol.Schema(
            {
                vol.Required(CONF_PERSONNUMMER): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                vol.Optional(CONF_REMEMBER, default=True): BooleanSelector(),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_PERSONNUMMER: user_input.get(CONF_PERSONNUMMER, ""),
                    CONF_REMEMBER: user_input.get(CONF_REMEMBER, True),
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"detail": detail},
        )

    async def async_step_lists(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._chosen = list(user_input.get(CONF_LISTS, []))
            return await self.async_step_mirror()
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_LISTS, default=[lst.id for lst in self._lists]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_list_options(self._lists, counts=True),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="lists",
            data_schema=schema,
            description_placeholders={"name": self._name or "ICA"},
        )

    async def async_step_mirror(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := _check_source(self.hass, user_input.get(CONF_MIRROR_SOURCE)):
                errors[CONF_MIRROR_SOURCE] = error
            else:
                return await self._async_finish(user_input)
        schema = vol.Schema(
            {
                vol.Optional(CONF_MIRROR_SOURCE): TODO_SELECTOR,
                vol.Required(CONF_MIRROR_LIST): SelectSelector(
                    SelectSelectorConfig(
                        options=_list_options(self._lists),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_MIRROR_IMPORT, default=False): BooleanSelector(),
            }
        )
        suggested = user_input or {
            CONF_MIRROR_SOURCE: _suggest_source(self.hass),
            CONF_MIRROR_LIST: _default_list(self._lists),
        }
        return self.async_show_form(
            step_id="mirror",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def _async_finish(self, mirror_input: Mapping[str, Any]) -> ConfigFlowResult:
        assert self._token is not None and self.unique_id is not None
        store: Store[dict[str, Any]] = Store(
            self.hass, STORAGE_VERSION, auth_store_key(self.unique_id), private=True
        )
        await store.async_save(self._token.as_dict())
        data: dict[str, Any] = {CONF_PERSONNUMMER: self._pnr, CONF_REMEMBER: self._remember}
        if self._remember:
            data[CONF_PASSWORD] = self._password
        options = {
            CONF_LISTS: self._chosen,
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            **_mirror_options(mirror_input),
        }
        title = f"ICA ({self._name})" if self._name else "ICA"
        return self.async_create_entry(title=title, data=data, options=options)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        pnr = entry.data[CONF_PERSONNUMMER]
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            try:
                token, _name, _lists = await _async_login(
                    self.hass, pnr, user_input[CONF_PASSWORD]
                )
            except Exception as err:  # noqa: BLE001
                errors["base"], detail = _error(err)
            else:
                store: Store[dict[str, Any]] = Store(
                    self.hass, STORAGE_VERSION, auth_store_key(str(entry.unique_id)), private=True
                )
                await store.async_save(token.as_dict())
                remember = bool(user_input.get(CONF_REMEMBER, True))
                data = {**entry.data, CONF_REMEMBER: remember}
                if remember:
                    data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
                else:
                    data.pop(CONF_PASSWORD, None)
                return self.async_update_reload_and_abort(entry, data=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                vol.Optional(
                    CONF_REMEMBER, default=entry.data.get(CONF_REMEMBER, True)
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "account": mask_personnummer(pnr),
                "detail": detail,
            },
        )


class IcaListsOptionsFlow(OptionsFlowWithReload):
    """Change lists, mirroring and how often to check ICA."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        runtime = getattr(entry, "runtime_data", None)
        data = runtime.coordinator.data if runtime is not None else None
        lists = list(data.values()) if data else []
        if not lists:
            return self.async_abort(reason="not_loaded")

        errors: dict[str, str] = {}
        if user_input is not None:
            if error := _check_source(self.hass, user_input.get(CONF_MIRROR_SOURCE)):
                errors[CONF_MIRROR_SOURCE] = error
            else:
                return self.async_create_entry(
                    data={
                        CONF_LISTS: list(user_input.get(CONF_LISTS, [])),
                        CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
                        **_mirror_options(user_input),
                    }
                )

        current = entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_LISTS): SelectSelector(
                    SelectSelectorConfig(
                        options=_list_options(lists, counts=True),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(CONF_MIRROR_SOURCE): TODO_SELECTOR,
                vol.Required(CONF_MIRROR_LIST): SelectSelector(
                    SelectSelectorConfig(
                        options=_list_options(lists), mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(CONF_MIRROR_IMPORT): BooleanSelector(),
                vol.Required(CONF_SCAN_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=30,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
            }
        )
        suggested = user_input or {
            CONF_LISTS: current.get(CONF_LISTS, [lst.id for lst in lists]),
            CONF_MIRROR_SOURCE: current.get(CONF_MIRROR_SOURCE),
            CONF_MIRROR_LIST: current.get(CONF_MIRROR_LIST) or _default_list(lists),
            CONF_MIRROR_IMPORT: current.get(CONF_MIRROR_IMPORT, False),
            CONF_SCAN_INTERVAL: current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )
