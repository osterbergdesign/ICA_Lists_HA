"""Run the integration inside a real Home Assistant core, without pytest plugins.

Needs Home Assistant's source and its Python dependencies importable. The
http, frontend, websocket_api and zeroconf components are swapped for tiny
stand-ins because the tests don't serve a UI or look up mDNS names.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types

HERE = Path(__file__).parent
COMPONENT_DIR = HERE.parent / "custom_components" / "ica_lists"


def _stub(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def install_stubs() -> bool:
    """Prepare imports. Returns False when Home Assistant isn't available."""
    try:
        from probatio.compat import install_as_voluptuous  # HA >= 2026.10
    except ImportError:
        pass
    else:
        install_as_voluptuous()
    try:
        import homeassistant.core  # noqa: F401
    except Exception:  # noqa: BLE001
        return False

    def _decorator(*_args, **_kwargs):
        return lambda func: func

    _stub(
        "homeassistant.components.websocket_api",
        ERR_NOT_FOUND="not_found",
        ERR_NOT_SUPPORTED="not_supported",
        ActiveConnection=object,
        websocket_command=_decorator,
        async_response=lambda func: func,
        require_admin=lambda func: func,
        ws_require_user=_decorator,
        async_register_command=lambda *a, **k: None,
        error_message=lambda *a, **k: {},
        event_message=lambda *a, **k: {},
        result_message=lambda *a, **k: {},
    )
    _stub("homeassistant.components.frontend", async_register_built_in_panel=lambda *a, **k: None)
    _stub("homeassistant.components.zeroconf", async_get_async_zeroconf=lambda hass: None)
    http = _stub(
        "homeassistant.components.http",
        HomeAssistantView=object,
        StaticPathConfig=object,
        require_admin=lambda func=None, **_kw: func if func else (lambda f: f),
        KEY_HASS="hass",
    )
    http.const = _stub("homeassistant.components.http.const", DATA_SUPERVISOR_USER="supervisor_user")
    return True


TEST_LIST_INIT = '"""Test-only in-memory to-do list."""\n'
TEST_LIST_MANIFEST = '{"domain": "test_list", "name": "Test list", "version": "1.0.0", "codeowners": [], "documentation": "https://example.com", "requirements": []}\n'
TEST_LIST_TODO = '''
"""An in-memory to-do list, like Google Keep's, for the mirror tests."""
import uuid
from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature

LISTS = {}


class MemoryList(TodoListEntity):
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, name):
        self._attr_name = name
        self._attr_unique_id = f"test_list_{name}"
        self._attr_todo_items = []
        self.quiet_fail = False  # like Google Keep: swallow update errors
        LISTS[name] = self

    def put(self, summary, done=False):
        item = TodoItem(uid=uuid.uuid4().hex[:8], summary=summary,
                        status=TodoItemStatus.COMPLETED if done else TodoItemStatus.NEEDS_ACTION)
        self._attr_todo_items = [*self._attr_todo_items, item]
        self.async_write_ha_state()
        return item

    def find(self, summary):
        return next((i for i in self._attr_todo_items if i.summary == summary), None)

    async def async_create_todo_item(self, item):
        self.put(item.summary)

    async def async_update_todo_item(self, item):
        if self.quiet_fail:
            return
        self._attr_todo_items = [item if i.uid == item.uid else i for i in self._attr_todo_items]
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids):
        self._attr_todo_items = [i for i in self._attr_todo_items if i.uid not in uids]
        self.async_write_ha_state()


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([MemoryList("Shopping list")])
'''


@asynccontextmanager
async def home_assistant():
    """A started Home Assistant with ica_lists and a test list available."""
    from homeassistant import config_entries, loader
    from homeassistant.core import CoreState, HomeAssistant
    from homeassistant.helpers import (
        area_registry as ar,
        category_registry as cr,
        condition,
        device_registry as dr,
        entity,
        entity_registry as er,
        floor_registry as fr,
        issue_registry as ir,
        label_registry as lr,
        translation,
        trigger,
    )

    config_dir = tempfile.mkdtemp(prefix="ica_lists_ha_")
    custom = Path(config_dir) / "custom_components"
    custom.mkdir()
    os.symlink(COMPONENT_DIR, custom / "ica_lists")
    test_list = custom / "test_list"
    test_list.mkdir()
    (test_list / "__init__.py").write_text(TEST_LIST_INIT)
    (test_list / "manifest.json").write_text(TEST_LIST_MANIFEST)
    (test_list / "todo.py").write_text(TEST_LIST_TODO)

    for name in [m for m in sys.modules if m == "custom_components" or m.startswith("custom_components.")]:
        del sys.modules[name]
    sys.path.insert(0, config_dir)

    hass = HomeAssistant(config_dir)
    hass.config.skip_pip = True
    await hass.config.async_set_time_zone("Europe/Stockholm")
    hass.config_entries = config_entries.ConfigEntries(hass, {"_": "not empty"})
    hass.config_entries._initialized.set()  # noqa: SLF001
    entity.async_setup(hass)
    loader.async_setup(hass)
    await condition.async_setup(hass)
    await trigger.async_setup(hass)
    hass.data[translation.TRANSLATION_FLATTEN_CACHE] = translation._TranslationCache(hass)  # noqa: SLF001
    dr.async_setup(hass)
    for registry in (ar, cr, dr, er, fr, ir, lr):
        await registry.async_load(hass)
    hass.data["bootstrap_registries_loaded"] = None  # bootstrap.DATA_REGISTRIES_LOADED
    hass.config.components.update({"http", "frontend", "websocket_api", "zeroconf"})
    hass.set_state(CoreState.running)
    try:
        yield hass
    finally:
        await hass.async_stop(force=True)
        sys.path.remove(config_dir)
        shutil.rmtree(config_dir, ignore_errors=True)


async def settle(hass, rounds: int = 3) -> None:
    for _ in range(rounds):
        await hass.async_block_till_done()
        await asyncio.sleep(0)
