"""A small fake of ICA's login server and shopping-list API for tests.

It follows the flow the real servers use (as documented by the projects in
NOTICE): bootstrap token, client registration, PKCE authorize, an HTML login
form, an auto-submitting grant form, a code on the app's own URL scheme, and
token exchange/refresh. It is deliberately strict so that client mistakes
(missing cookie, wrong verifier, 10-digit personnummer...) fail loudly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from urllib.parse import urlencode

from aiohttp import web

DCR_ID = "ica-app-dcr-registration"
PNR = "198112189876"  # Skatteverket's example number, valid check digit
PASSWORD = "hemligt"


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


class FakeIca:
    def __init__(self) -> None:
        self.app = web.Application()
        r = self.app.router
        r.add_post("/oauth/v2/token", self.token)
        r.add_post("/register", self.register)
        r.add_get("/oauth/v2/authorize", self.authorize_get)
        r.add_post("/oauth/v2/authorize", self.authorize_post)
        r.add_get("/authn/authenticate/IcaCustomers", self.login_page)
        r.add_post("/authn/authenticate/IcaCustomers", self.login_post)
        base = "/sverige/digx/shopping-list/v1/api"
        r.add_get(f"{base}/list/all", self.lists)
        r.add_post(f"{base}/list/{{list_id}}/row", self.add_row)
        r.add_put(f"{base}/row/{{row_id}}", self.put_row)
        r.add_delete(f"{base}/row/{{row_id}}", self.delete_row)
        self.runner: web.AppRunner | None = None
        self.base = ""
        # behaviour switches
        self.geo_block = False
        self.direct_code = False  # login POST redirects straight to the app with a code
        self.access_lifetime = 900
        self.rotate_refresh = True
        self.reject_refresh = False
        self.kill_access = False  # API answers 401 to the current access token once
        self.grant_form_without_action = False
        # state
        self.clients: dict[str, dict] = {}
        self.boot_tokens: set[str] = set()
        self.sessions: dict[str, dict] = {}
        self.sso: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.access: dict[str, float] = {}
        self.refresh: dict[str, str] = {}
        self.calls: list[str] = []
        self.login_posts = 0
        self.full_logins = 0
        self.refreshes = 0
        self.lists_data = [
            {
                "id": "L-HANDLA",
                "name": "Handla",
                "rows": [
                    {"id": "R1", "text": "Mjölk", "isStriked": False,
                     "quantity": {"amount": 2, "unit": "st"},
                     "article": {"id": 1, "group": {"id": 5, "name": "Mejeri"}},
                     "order": 1, "shoppingListRowId": 11},
                    {"id": "R2", "text": "Bröd", "isStriked": True, "quantity": None,
                     "article": None, "order": 2, "shoppingListRowId": 12},
                ],
            },
            {"id": "L-FEST", "name": "Fest", "rows": []},
        ]

    async def start(self) -> str:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}"
        return self.base

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    # ---------------------------------------------------------------- helpers
    def _geo(self) -> None:
        if self.geo_block:
            raise web.HTTPUnavailableForLegalReasons(link=None)

    def _bearer(self, request: web.Request) -> str:
        auth = request.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def _api_auth(self, request: web.Request) -> None:
        self._geo()
        token = self._bearer(request)
        if self.kill_access and token in self.access:
            self.kill_access = False
            self.access.pop(token, None)
        exp = self.access.get(token)
        if exp is None or exp < time.time():
            raise web.HTTPUnauthorized()

    def _issue(self, client_id: str, scope: str, with_id_token: bool) -> dict:
        access = "AT-" + secrets.token_hex(16)
        refresh = "RT-" + secrets.token_hex(16)
        self.access[access] = time.time() + self.access_lifetime
        self.refresh[refresh] = client_id
        data = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": self.access_lifetime,
            "token_type": "bearer",
            "scope": scope,
        }
        if with_id_token:
            data["id_token"] = ".".join(
                [_b64({"alg": "none"}), _b64({"given_name": "Max", "family_name": "Testsson"}), "sig"]
            )
        return data

    # ---------------------------------------------------------------- login
    async def token(self, request: web.Request) -> web.Response:
        self._geo()
        form = await request.post()
        grant = form.get("grant_type")
        self.calls.append(f"token:{grant}")
        if grant == "client_credentials":
            if form.get("client_id") != DCR_ID or not form.get("client_secret") or form.get("scope") != "dcr":
                return web.json_response({"error": "invalid_client"}, status=401)
            boot = "BOOT-" + secrets.token_hex(8)
            self.boot_tokens.add(boot)
            return web.json_response({"access_token": boot, "expires_in": 300})
        if grant == "authorization_code":
            code = self.codes.pop(str(form.get("code")), None)
            if code is None:
                return web.json_response({"error": "invalid_grant"}, status=400)
            client = self.clients.get(str(form.get("client_id")))
            if client is None or client["client_secret"] != form.get("client_secret"):
                return web.json_response({"error": "invalid_client"}, status=401)
            verifier = str(form.get("code_verifier"))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if challenge != code["challenge"] or form.get("redirect_uri") != "icacurity://app":
                return web.json_response({"error": "invalid_grant"}, status=400)
            self.full_logins += 1
            return web.json_response(self._issue(client["client_id"], client["scope"], True))
        if grant == "refresh_token":
            self.refreshes += 1
            auth = request.headers.get("Authorization", "")
            try:
                client_id, secret = base64.b64decode(auth[6:]).decode().split(":", 1)
            except Exception:  # noqa: BLE001
                return web.json_response({"error": "invalid_client"}, status=401)
            client = self.clients.get(client_id)
            if client is None or client["client_secret"] != secret:
                return web.json_response({"error": "invalid_client"}, status=401)
            rt = str(form.get("refresh_token"))
            if self.reject_refresh or self.refresh.get(rt) != client_id:
                return web.json_response({"error": "invalid_grant"}, status=400)
            data = self._issue(client_id, client["scope"], False)
            if self.rotate_refresh:
                self.refresh.pop(rt, None)
            else:
                self.refresh.pop(data["refresh_token"], None)
                data.pop("refresh_token")
            return web.json_response(data)
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    async def register(self, request: web.Request) -> web.Response:
        self._geo()
        self.calls.append("register")
        if self._bearer(request) not in self.boot_tokens:
            return web.json_response({"error": "invalid_token"}, status=401)
        body = await request.json()
        if body.get("software_id") != "dcr-ica-app-template":
            return web.json_response({"error": "invalid_software"}, status=400)
        client = {
            "client_id": "C-" + uuid.uuid4().hex[:8],
            "client_secret": secrets.token_hex(12),
            "scope": "openid shoppinglist",
        }
        self.clients[client["client_id"]] = client
        return web.json_response(client, status=201)

    async def authorize_get(self, request: web.Request) -> web.Response:
        self._geo()
        self.calls.append("authorize")
        q = request.query
        client = self.clients.get(q.get("client_id", ""))
        if (client is None or q.get("response_type") != "code" or q.get("code_challenge_method") != "S256"
                or not q.get("code_challenge") or q.get("redirect_uri") != "icacurity://app"):
            return web.Response(status=400, text="bad authorize")
        sid = secrets.token_hex(8)
        state = secrets.token_hex(6)
        self.sessions[sid] = {"client_id": client["client_id"], "challenge": q["code_challenge"], "state": state, "csrf": secrets.token_hex(4)}
        resp = web.HTTPFound(f"/authn/authenticate/IcaCustomers?{urlencode({'serviceProviderId': 'x', 'state': state})}")
        resp.set_cookie("curity-session", sid)
        raise resp

    def _session(self, request: web.Request) -> dict | None:
        return self.sessions.get(request.cookies.get("curity-session", ""))

    def _login_html(self, session: dict, error: str = "") -> str:
        return f"""<html><body>
<form method="post" action="/authn/authenticate/IcaCustomers" id="bankid"><input type="hidden" name="method" value="bankid"></form>
<p class="error">{error}</p>
<form method="post" action="/authn/authenticate/IcaCustomers?serviceProviderId=x">
  <input type="hidden" name="csrf" value="{session['csrf']}">
  <input type="text" name="userName" value="">
  <input type="password" name="password">
  <input type="checkbox" name="remember" value="on">
  <button type="submit">Logga in</button>
</form></body></html>"""

    async def login_page(self, request: web.Request) -> web.Response:
        self._geo()
        session = self._session(request)
        if session is None:
            return web.Response(status=400, text="no session")
        return web.Response(text=self._login_html(session), content_type="text/html")

    async def login_post(self, request: web.Request) -> web.Response:
        self._geo()
        self.login_posts += 1
        session = self._session(request)
        form = await request.post()
        if session is None or form.get("csrf") != session["csrf"]:
            return web.Response(status=400, text="bad session")
        pnr = str(form.get("userName", ""))
        if len(pnr) != 12:
            return web.Response(status=400, text="")
        if pnr != PNR or form.get("password") != PASSWORD:
            return web.Response(text=self._login_html(session, "Fel personnummer eller lösenord"), content_type="text/html")
        token = secrets.token_hex(10)
        self.sso[token] = session
        if self.direct_code:
            code = secrets.token_hex(8)
            self.codes[code] = session
            raise web.HTTPFound(f"icacurity://app?code={code}&state={session['state']}")
        action = "" if self.grant_form_without_action else (
            f"{self.base}/oauth/v2/authorize?" + urlencode({"client_id": session["client_id"], "forceAuthN": "true", "acr": "urn:se:curity:authentication:html-form:IcaCustomers"})
        )
        action_attr = f' action="{action}"' if action else ""
        html = f"""<html><body onload="document.forms[0].submit()">
<form method="post"{action_attr}><input value="{token}" type="hidden" name="token"><input type="hidden" name="state" value="{session['state']}"></form>
</body></html>"""
        return web.Response(text=html, content_type="text/html")

    async def authorize_post(self, request: web.Request) -> web.Response:
        self._geo()
        self.calls.append("authorize:post")
        form = await request.post()
        session = self.sso.pop(str(form.get("token")), None)
        if session is None or form.get("state") != session["state"] or request.query.get("forceAuthN") != "true":
            return web.Response(status=400, text="bad grant")
        code = secrets.token_hex(8)
        self.codes[code] = session
        raise web.HTTPFound(f"icacurity://app?code={code}&state={session['state']}")

    # ---------------------------------------------------------------- lists
    def _find_row(self, row_id: str) -> tuple[dict, dict] | None:
        for lst in self.lists_data:
            for row in lst["rows"]:
                if row["id"] == row_id:
                    return lst, row
        return None

    async def lists(self, request: web.Request) -> web.Response:
        self._api_auth(request)
        self.calls.append("lists")
        return web.json_response(self.lists_data)

    async def add_row(self, request: web.Request) -> web.Response:
        self._api_auth(request)
        lst = next((x for x in self.lists_data if x["id"] == request.match_info["list_id"]), None)
        if lst is None:
            raise web.HTTPNotFound()
        body = await request.json()
        row = {
            "id": "R-" + uuid.uuid4().hex[:6],
            "text": body["text"],
            "isStriked": bool(body.get("isStriked")),
            "quantity": body.get("quantity") or None,
            "article": {"id": 9, "group": {"id": 1, "name": "Övrigt"}},
            "order": len(lst["rows"]) + 1,
            "shoppingListRowId": 100 + len(lst["rows"]),
        }
        lst["rows"].append(row)
        self.calls.append(f"add:{body['text']}")
        return web.json_response(row)

    async def put_row(self, request: web.Request) -> web.Response:
        self._api_auth(request)
        found = self._find_row(request.match_info["row_id"])
        if found is None:
            raise web.HTTPNotFound()
        lst, row = found
        body = await request.json()
        # ICA replaces the row: whatever is missing is gone.
        new = {k: body.get(k) for k in ("id", "text", "isStriked", "quantity", "article", "order", "shoppingListRowId")}
        lst["rows"][lst["rows"].index(row)] = new
        self.calls.append(f"put:{new['text']}:{new['isStriked']}")
        return web.json_response(new)

    async def delete_row(self, request: web.Request) -> web.Response:
        self._api_auth(request)
        found = self._find_row(request.match_info["row_id"])
        if found is None:
            raise web.HTTPNotFound()
        lst, row = found
        lst["rows"].remove(row)
        self.calls.append(f"delete:{row['text']}")
        return web.Response(status=200)
