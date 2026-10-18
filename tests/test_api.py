"""Tests for the ICA client against the fake ICA server (real aiohttp, real HTTP)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
import time

import aiohttp
from fake_ica import PASSWORD, PNR, FakeIca
from run import COMPONENT, load_module

api = load_module("ica_api", COMPONENT / "ica_api.py")


@asynccontextmanager
async def fake_env(**switches):
    fake = FakeIca()
    for key, value in switches.items():
        setattr(fake, key, value)
    base = await fake.start()
    # unsafe=True lets the jar keep cookies for 127.0.0.1 (real ICA uses hostnames)
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    try:
        yield fake, base, session
    finally:
        await session.close()
        await fake.stop()


def make(session, base, **kw):
    auth = api.IcaAuth(session, ims_base=base, **kw)
    return auth, api.IcaClient(session, auth, api_base=base)


# ---------------------------------------------------------------- helpers
def test_personnummer_helpers():
    today = date(2026, 10, 18)
    assert api.normalize_personnummer("811218-9876", today) == "198112189876"
    assert api.normalize_personnummer("19811218-9876", today) == "198112189876"
    assert api.normalize_personnummer("0101012384", today) == "200101012384"
    assert api.normalize_personnummer("121212+1212", today) == "191212121212"
    assert api.personnummer_valid("198112189876")
    assert not api.personnummer_valid("198112189875")
    assert not api.personnummer_valid("8112189876")
    assert api.mask_personnummer("198112189876") == "19811218-XXXX"
    assert api.account_id("198112189876") == api.account_id("198112189876")
    assert "198112189876" not in api.account_id("198112189876")


def test_sanitize_strips_secrets():
    text = api.sanitize("pnr 198112189876 token AT-0123456789abcdef0123456789abcdef ok")
    assert "198112189876" not in text and "0123456789abcdef0123456789abcdef" not in text
    assert text.endswith("ok")


# ---------------------------------------------------------------- login
async def test_login_and_lists():
    async with fake_env() as (fake, base, session):
        saved = []

        async def on_token(token):
            saved.append(token)

        auth, client = make(session, base, on_token=on_token)
        token = await auth.async_login("811218-9876", PASSWORD)  # 10 digits on purpose
        assert token.refresh_token.startswith("RT-") and token.client_id.startswith("C-")
        assert auth.account_name == "Max Testsson"
        assert saved and saved[-1] is token
        assert fake.login_posts == 1 and fake.full_logins == 1
        lists = await client.async_get_lists()
        assert [lst.name for lst in lists] == ["Handla", "Fest"]
        handla = lists[0]
        assert handla.row("R1").quantity_text == "2 st" and handla.row("R2").striked


async def test_wrong_password_is_one_attempt():
    async with fake_env() as (fake, base, session):
        auth, _ = make(session, base)
        try:
            await auth.async_login(PNR, "fel")
        except api.IcaAuthError:
            pass
        else:
            raise AssertionError("expected IcaAuthError")
        assert fake.login_posts == 1


async def test_direct_code_redirect_and_formless_grant():
    async with fake_env(direct_code=True) as (fake, base, session):
        auth, client = make(session, base)
        await auth.async_login(PNR, PASSWORD)
        assert await client.async_get_lists()
    async with fake_env(grant_form_without_action=True) as (fake, base, session):
        auth, client = make(session, base)
        await auth.async_login(PNR, PASSWORD)
        assert "authorize:post" in fake.calls


async def test_geo_block():
    async with fake_env(geo_block=True) as (fake, base, session):
        auth, _ = make(session, base)
        try:
            await auth.async_login(PNR, PASSWORD)
        except api.IcaGeoBlockedError:
            pass
        else:
            raise AssertionError("expected IcaGeoBlockedError")


# ---------------------------------------------------------------- tokens
async def test_refresh_when_expired_and_saved():
    async with fake_env() as (fake, base, session):
        saved = []

        async def on_token(token):
            saved.append(token)

        auth, client = make(session, base, on_token=on_token)
        first = await auth.async_login(PNR, PASSWORD)
        first.expires_at = time.time()  # pretend it ran out
        await client.async_get_lists()
        assert fake.refreshes == 1 and fake.full_logins == 1
        assert saved[-1].access_token != first.access_token
        assert saved[-1].refresh_token != first.refresh_token  # rotated and kept


async def test_refresh_keeps_old_refresh_token_when_not_rotated():
    async with fake_env(rotate_refresh=False) as (fake, base, session):
        auth, client = make(session, base)
        first = await auth.async_login(PNR, PASSWORD)
        old_refresh = first.refresh_token
        first.expires_at = time.time()
        await client.async_get_lists()
        assert auth.token.refresh_token == old_refresh


async def test_api_401_forces_refresh_and_retries():
    async with fake_env() as (fake, base, session):
        auth, client = make(session, base)
        await auth.async_login(PNR, PASSWORD)
        fake.kill_access = True
        lists = await client.async_get_lists()
        assert lists and fake.refreshes == 1


async def test_restored_token_works_without_password():
    async with fake_env() as (fake, base, session):
        auth, _ = make(session, base)
        token = await auth.async_login(PNR, PASSWORD)
        restored = api.IcaToken.from_dict(token.as_dict())
        auth2, client2 = make(session, base, token=restored)
        assert await client2.async_get_lists()
        assert fake.full_logins == 1


async def test_refresh_refused_relogs_with_saved_password_once():
    async with fake_env() as (fake, base, session):
        auth, client = make(session, base, credentials=(PNR, PASSWORD))
        token = await auth.async_login(PNR, PASSWORD)
        token.expires_at = time.time()
        fake.reject_refresh = True
        assert await client.async_get_lists()
        assert fake.full_logins == 2


async def test_refresh_refused_without_password_needs_reauth():
    async with fake_env() as (fake, base, session):
        auth, client = make(session, base)
        token = await auth.async_login(PNR, PASSWORD)
        token.expires_at = time.time()
        fake.reject_refresh = True
        try:
            await client.async_get_lists()
        except api.IcaAuthError:
            pass
        else:
            raise AssertionError("expected IcaAuthError")


async def test_wrong_saved_password_never_retries():
    async with fake_env() as (fake, base, session):
        auth, client = make(session, base, credentials=(PNR, "byttlösen"))
        token = await auth.async_login(PNR, PASSWORD)
        token.expires_at = time.time()
        fake.reject_refresh = True
        for _ in range(3):
            try:
                await client.async_get_lists()
            except api.IcaAuthError:
                pass
            else:
                raise AssertionError("expected IcaAuthError")
        assert fake.login_posts == 2  # the setup login + exactly one automatic try


# ---------------------------------------------------------------- rows
async def test_add_update_delete_rows():
    async with fake_env() as (fake, base, session):
        auth, client = make(session, base)
        await auth.async_login(PNR, PASSWORD)
        row = await client.async_add_row("L-HANDLA", "Ägg")
        assert row and row.text == "Ägg" and not row.striked
        lists = await client.async_get_lists()
        mjolk = lists[0].row("R1")
        updated = await client.async_update_row(mjolk, striked=True)
        assert updated.striked and updated.text == "Mjölk"
        # the full row went back, so ICA kept the quantity and category
        server_row = fake._find_row("R1")[1]  # noqa: SLF001
        assert server_row["quantity"] == {"amount": 2, "unit": "st"}
        assert server_row["article"]["group"]["name"] == "Mejeri"
        renamed = await client.async_update_row(updated, text="Mjölk 3%")
        assert renamed.text == "Mjölk 3%" and renamed.striked
        await client.async_delete_row(row.id)
        await client.async_delete_row(row.id)  # already gone: still fine
        names = [r.text for r in (await client.async_get_lists())[0].rows]
        assert "Ägg" not in names


async def test_update_missing_row_raises_404():
    async with fake_env() as (fake, base, session):
        auth, client = make(session, base)
        await auth.async_login(PNR, PASSWORD)
        ghost = api.IcaRow(id="NOPE", text="x", striked=False, raw={"id": "NOPE", "text": "x"})
        try:
            await client.async_update_row(ghost, striked=True)
        except api.IcaApiError as err:
            assert err.status == 404
        else:
            raise AssertionError("expected IcaApiError")
