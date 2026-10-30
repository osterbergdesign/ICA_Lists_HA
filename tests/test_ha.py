"""The integration inside a real Home Assistant core, against the fake ICA.

Skipped when Home Assistant can't be imported (see ha_harness.py).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import importlib
import json
import sys

from fake_ica import PASSWORD, PNR, FakeIca
from ha_harness import home_assistant, install_stubs, settle

HAVE_HA = install_stubs()

if HAVE_HA:
    from homeassistant.components.todo import TodoItemStatus
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.setup import async_setup_component

DOMAIN = "ica_lists"
SHOPPING = "todo.shopping_list"


def skip_without_ha(func):
    async def wrapper():
        if not HAVE_HA:
            print("   (skipped: Home Assistant not importable)")
            return
        await func()

    wrapper.__name__ = func.__name__
    return wrapper


@asynccontextmanager
async def env():
    fake = FakeIca()
    await fake.start()
    base = fake.base.replace("127.0.0.1", "localhost")  # cookie jar ignores bare IPs
    fake.base = base
    try:
        async with home_assistant() as hass:
            yield hass, fake, base
    finally:
        await fake.stop()


def point_at(base: str) -> None:
    api = sys.modules["custom_components.ica_lists.ica_api"]
    api.IMS_BASE = base
    api.API_BASE = base
    api.API_BASES = (base,)


async def start_flow(hass, base):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    point_at(base)
    return result


async def add_entry(hass, base, *, remember=True, lists=None, mirror=None, password=PASSWORD):
    result = await start_flow(hass, base)
    assert result["type"] == "form" and result["step_id"] == "user", result
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"personnummer": "811218-9876", "password": password, "remember_password": remember},
    )
    assert result["step_id"] == "lists", result
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lists": lists if lists is not None else ["L-HANDLA"]}
    )
    assert result["step_id"] == "mirror", result
    data = {"mirror_list": "L-HANDLA", "mirror_import": False}
    if mirror:
        data["mirror_source"] = mirror
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] == "create_entry", result
    await settle(hass)
    return hass.config_entries.async_entries(DOMAIN)[0]


async def memory_list(hass):
    assert await async_setup_component(hass, "todo", {"todo": [{"platform": "test_list"}]})
    await settle(hass)
    return sys.modules["custom_components.test_list.todo"].LISTS["Shopping list"]


def rows(fake, list_index=0):
    return fake.lists_data[list_index]["rows"]


def row_named(fake, text):
    return next((r for r in rows(fake) if r["text"] == text), None)


# ----------------------------------------------------------------------------
@skip_without_ha
async def test_setup_flow_creates_todo_lists():
    async with env() as (hass, fake, base):
        entry = await add_entry(hass, base)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.title == "ICA (Max Testsson)"
        assert entry.data["personnummer"] == PNR and entry.data["password"] == PASSWORD
        state = hass.states.get("todo.ica_handla")
        assert state is not None and state.state == "1", state
        assert hass.states.get("todo.ica_fest") is None  # not chosen
        # the token was saved, so a restart doesn't need the password
        store = sys.modules["homeassistant.helpers.storage"].Store(hass, 1, f"{DOMAIN}.{entry.unique_id}.auth")
        saved = await store.async_load()
        assert saved and saved["refresh_token"].startswith("RT-")
        # a restart signs in with the saved token, not the password
        logins = fake.login_posts
        await hass.config_entries.async_reload(entry.entry_id)
        await settle(hass)
        assert entry.state is ConfigEntryState.LOADED and fake.login_posts == logins
        # an expired token is refreshed and the new one saved
        entry.runtime_data.auth.token.expires_at = 0
        await entry.runtime_data.coordinator.async_refresh()
        assert fake.refreshes == 1 and fake.login_posts == logins
        assert (await store.async_load())["access_token"] == entry.runtime_data.auth.token.access_token


@skip_without_ha
async def test_wrong_password_and_bad_personnummer_show_errors():
    async with env() as (hass, fake, base):
        result = await start_flow(hass, base)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"personnummer": "811218-9875", "password": "x"}
        )
        assert result["errors"] == {"personnummer": "invalid_personnummer"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"personnummer": PNR, "password": "fel"}
        )
        assert result["errors"] == {"base": "invalid_auth"}
        fake.geo_block = True
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"personnummer": PNR, "password": PASSWORD}
        )
        assert result["errors"] == {"base": "geo_blocked"}


@skip_without_ha
async def test_todo_actions_change_ica():
    async with env() as (hass, fake, base):
        await add_entry(hass, base)
        call = hass.services.async_call
        await call("todo", "add_item", {"item": "Ägg"}, target={"entity_id": "todo.ica_handla"}, blocking=True)
        assert row_named(fake, "Ägg") is not None
        await call("todo", "update_item", {"item": "Mjölk", "status": "completed"}, target={"entity_id": "todo.ica_handla"}, blocking=True)
        mjolk = row_named(fake, "Mjölk")
        assert mjolk["isStriked"] is True and mjolk["quantity"] == {"amount": 2, "unit": "st"}
        await call("todo", "remove_item", {"item": ["Ägg"]}, target={"entity_id": "todo.ica_handla"}, blocking=True)
        assert row_named(fake, "Ägg") is None
        await settle(hass)
        assert hass.states.get("todo.ica_handla").state == "0"
        response = await call("todo", "get_items", {}, target={"entity_id": "todo.ica_handla"}, blocking=True, return_response=True)
        items = response["todo.ica_handla"]["items"]
        assert any(i["summary"] == "Mjölk" and i.get("description") == "2 st" for i in items)


@skip_without_ha
async def test_mirror_end_to_end():
    async with env() as (hass, fake, base):
        mem = await memory_list(hass)
        mem.put("mjölk")  # same as ICA's open "Mjölk": linked, not added again
        mem.put("Kaffe", done=True)  # done: left alone
        mem.put("Ägg")
        entry = await add_entry(hass, base, mirror=SHOPPING)
        mirror = entry.runtime_data.mirror
        coordinator = entry.runtime_data.coordinator
        await mirror.async_sync()
        texts = [r["text"] for r in rows(fake)]
        assert texts.count("Mjölk") == 1 and "mjölk" not in texts and "Kaffe" not in texts
        assert texts.count("Ägg") == 1

        # ticked off in the ICA app -> ticked off in HA
        row_named(fake, "Ägg")["isStriked"] = True
        await coordinator.async_refresh()
        await mirror.async_sync()
        assert mem.find("Ägg").status == TodoItemStatus.COMPLETED

        # renamed in HA -> renamed in ICA, keeping ICA's quantity
        await hass.services.async_call("todo", "update_item", {"item": "mjölk", "rename": "Mjölk 3%"}, target={"entity_id": SHOPPING}, blocking=True)
        await mirror.async_sync()
        renamed = row_named(fake, "Mjölk 3%")
        assert renamed is not None and renamed["quantity"] == {"amount": 2, "unit": "st"}

        # removed in HA -> removed from ICA
        await hass.services.async_call("todo", "remove_item", {"item": ["Mjölk 3%"]}, target={"entity_id": SHOPPING}, blocking=True)
        await mirror.async_sync()
        assert row_named(fake, "Mjölk 3%") is None

        # removed in ICA -> ticked off in HA
        mem.put("Ost")
        await mirror.async_sync()
        assert row_named(fake, "Ost") is not None
        rows(fake).remove(row_named(fake, "Ost"))
        await coordinator.async_refresh()
        await mirror.async_sync()
        assert mem.find("Ost").status == TodoItemStatus.COMPLETED

        # nothing is added twice, even after a reload
        await hass.config_entries.async_reload(entry.entry_id)
        await settle(hass)
        await entry.runtime_data.mirror.async_sync()
        texts = [r["text"] for r in rows(fake)]
        assert texts.count("Ägg") == 1 and texts.count("Ost") == 0
        assert sum(1 for c in fake.calls if c == "add:Ägg") == 1


@skip_without_ha
async def test_mirror_never_undoes_ica_when_ha_ignores_changes():
    async with env() as (hass, fake, base):
        mem = await memory_list(hass)
        mem.put("Smör")
        entry = await add_entry(hass, base, mirror=SHOPPING)
        mirror = entry.runtime_data.mirror
        await mirror.async_sync()
        mem.quiet_fail = True  # like Google Keep when Google is unreachable
        row_named(fake, "Smör")["isStriked"] = True
        for _ in range(5):
            await entry.runtime_data.coordinator.async_refresh()
            await mirror.async_sync()
        assert row_named(fake, "Smör")["isStriked"] is True
        assert not any(c.startswith("put:Smör:False") for c in fake.calls)


@skip_without_ha
async def test_big_removal_waits():
    async with env() as (hass, fake, base):
        mem = await memory_list(hass)
        for name in ("A", "B", "C", "D", "E", "F"):
            mem.put(name)
        entry = await add_entry(hass, base, mirror=SHOPPING)
        mirror = entry.runtime_data.mirror
        await mirror.async_sync()
        assert all(row_named(fake, n) for n in "ABCDEF")
        await hass.services.async_call("todo", "remove_item", {"item": list("ABCDEF")}, target={"entity_id": SHOPPING}, blocking=True)
        await mirror.async_sync()
        assert all(row_named(fake, n) for n in "ABCDEF")  # held for now
        mirror._hold_since -= 600  # noqa: SLF001 - pretend the hold has passed
        await mirror.async_sync()
        assert not any(row_named(fake, n) for n in "ABCDEF")


@skip_without_ha
async def test_reauth_when_login_expires():
    async with env() as (hass, fake, base):
        entry = await add_entry(hass, base, remember=False)
        assert "password" not in entry.data
        fake.reject_refresh = True
        entry.runtime_data.auth.token.expires_at = 0
        await entry.runtime_data.coordinator.async_refresh()
        await settle(hass)
        flows = [f for f in hass.config_entries.flow.async_progress() if f["context"].get("source") == "reauth"]
        assert flows, "a reauth flow should have started"
        fake.reject_refresh = False
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {"password": PASSWORD, "remember_password": True}
        )
        assert result["type"] == "abort" and result["reason"] == "reauth_successful", result
        await settle(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.state is ConfigEntryState.LOADED and entry.data["password"] == PASSWORD


@skip_without_ha
async def test_options_change_lists_and_mirror():
    async with env() as (hass, fake, base):
        mem = await memory_list(hass)
        entry = await add_entry(hass, base)
        assert entry.runtime_data.mirror is None
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == "form", result
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"lists": ["L-HANDLA", "L-FEST"], "mirror_source": SHOPPING, "mirror_list": "L-FEST", "mirror_import": True, "scan_interval": 5},
        )
        assert result["type"] == "create_entry", result
        await settle(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert hass.states.get("todo.ica_fest") is not None
        assert entry.runtime_data.mirror is not None and entry.runtime_data.mirror.list_id == "L-FEST"
        # import: an item added in the ICA app shows up in HA
        fake.lists_data[1]["rows"].append({"id": "RF1", "text": "Chips", "isStriked": False, "quantity": None, "article": None, "order": 1, "shoppingListRowId": 900})
        await entry.runtime_data.coordinator.async_refresh()
        await entry.runtime_data.mirror.async_sync()
        assert mem.find("Chips") is not None
        await entry.runtime_data.mirror.async_sync()
        assert sum(1 for i in mem.todo_items if i.summary == "Chips") == 1
        assert not any(c == "add:Chips" for c in fake.calls)
        # the mirror can't point at the ICA lists themselves
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"lists": ["L-HANDLA"], "mirror_source": "todo.ica_handla", "mirror_list": "L-HANDLA", "scan_interval": 2}
        )
        assert result["errors"] == {"mirror_source": "mirror_self"}


@skip_without_ha
async def test_diagnostics_hold_no_secrets():
    async with env() as (hass, fake, base):
        mem = await memory_list(hass)
        mem.put("Bananer")
        entry = await add_entry(hass, base, mirror=SHOPPING)
        await entry.runtime_data.mirror.async_sync()
        diagnostics = importlib.import_module("custom_components.ica_lists.diagnostics")
        data = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        text = json.dumps(data, default=str)
        token = entry.runtime_data.auth.token
        for secret in (PNR, "811218", PASSWORD, token.access_token, token.refresh_token, token.client_secret):
            assert secret not in text, secret
        assert data["mirror"]["pairs"] >= 1 and data["lists"]


@skip_without_ha
async def test_unload_and_remove_clean_up():
    async with env() as (hass, fake, base):
        entry = await add_entry(hass, base)
        assert await hass.config_entries.async_remove(entry.entry_id)
        await settle(hass)
        store = sys.modules["homeassistant.helpers.storage"].Store(hass, 1, f"{DOMAIN}.{entry.unique_id}.auth")
        assert await store.async_load() is None
