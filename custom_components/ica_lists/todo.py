"""ICA shopping lists as Home Assistant to-do lists."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LISTS, DOMAIN
from .coordinator import IcaConfigEntry, IcaCoordinator
from .ica_api import IcaError, IcaList

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IcaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a to-do list for each chosen ICA list."""
    coordinator = entry.runtime_data.coordinator
    wanted = entry.options.get(CONF_LISTS)
    added: set[str] = set()

    if wanted is not None:
        # Lists that were un-ticked in the options: remove their old entities.
        registry = er.async_get(hass)
        keep = {f"{entry.unique_id}_{list_id}" for list_id in wanted}
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if reg_entry.domain == "todo" and reg_entry.unique_id not in keep:
                registry.async_remove(reg_entry.entity_id)

    @callback
    def _add_lists() -> None:
        new = []
        for list_id in (coordinator.data or {}):
            if list_id in added or (wanted is not None and list_id not in wanted):
                continue
            added.add(list_id)
            new.append(IcaTodoList(coordinator, entry, list_id))
        if new:
            async_add_entities(new)

    _add_lists()
    entry.async_on_unload(coordinator.async_add_listener(_add_lists))


class IcaTodoList(CoordinatorEntity[IcaCoordinator], TodoListEntity):
    """One ICA shopping list."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(
        self, coordinator: IcaCoordinator, entry: IcaConfigEntry, list_id: str
    ) -> None:
        super().__init__(coordinator)
        self._list_id = list_id
        self._attr_unique_id = f"{entry.unique_id}_{list_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name="ICA",
            manufacturer="ICA",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.ica.se/inkopslistor/",
        )
        self._refresh_from_list()

    @property
    def _list(self) -> IcaList | None:
        return (self.coordinator.data or {}).get(self._list_id)

    @property
    def available(self) -> bool:
        return super().available and self._list is not None

    def _refresh_from_list(self) -> None:
        lst = self._list
        if lst is None:
            self._attr_todo_items = None
            return
        self._attr_name = lst.name
        self._attr_todo_items = [
            TodoItem(
                uid=row.id,
                summary=row.text,
                status=TodoItemStatus.COMPLETED if row.striked else TodoItemStatus.NEEDS_ACTION,
                description=row.quantity_text,
            )
            for row in lst.rows
        ]

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_from_list()
        super()._handle_coordinator_update()

    async def async_create_todo_item(self, item: TodoItem) -> None:
        text = (item.summary or "").strip()
        if not text:
            return
        try:
            row = await self.coordinator.client.async_add_row(self._list_id, text)
        except IcaError as err:
            raise HomeAssistantError(f"Couldn't add '{text}' to ICA: {err}") from err
        if row is not None:
            self.coordinator.apply_row(self._list_id, row)
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        lst = self._list
        row = lst.row(item.uid) if lst else None
        if row is None:
            raise HomeAssistantError("That item isn't on the ICA list any more")
        striked = item.status == TodoItemStatus.COMPLETED
        text = (item.summary or "").strip() or None
        if text == row.text:
            text = None
        if text is None and striked == row.striked:
            return
        try:
            updated = await self.coordinator.client.async_update_row(
                row, text=text, striked=striked
            )
        except IcaError as err:
            raise HomeAssistantError(f"Couldn't update '{row.text}' in ICA: {err}") from err
        self.coordinator.apply_row(self._list_id, updated)
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            try:
                await self.coordinator.client.async_delete_row(uid)
            except IcaError as err:
                raise HomeAssistantError(f"Couldn't remove the item from ICA: {err}") from err
            self.coordinator.remove_row(self._list_id, uid)
        await self.coordinator.async_request_refresh()
