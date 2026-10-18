"""Async client for ICA's shopping lists (unofficial API).

Kept free of Home Assistant imports so it can be tested on its own.

Login uses the same OAuth2 flow as the ICA app, on ICA's Curity login server
(ims.icagruppen.se): register a client, start a PKCE authorization, post the
personnummer and password to the login form, then swap the code for tokens.
That gives a short access token (about 15 minutes) and a long-lived refresh
token, so the password is only needed again if the refresh token stops working.

Lists use ICA's web shopping-list service on apimgw-pub.ica.se.

The protocol was worked out by kanylbullen/ica-mcp (MIT),
daha/ha-ica-shopping-barcode (MIT) and whiteout12/ha-ica-shopping-list (MIT).
See NOTICE.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
import dataclasses
from dataclasses import dataclass, field
from datetime import date
import hashlib
from html.parser import HTMLParser
import json
import logging
import re
import secrets
import string
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

IMS_BASE = "https://ims.icagruppen.se"
API_BASE = "https://apimgw-pub.ica.se"

TOKEN_PATH = "/oauth/v2/token"
AUTHORIZE_PATH = "/oauth/v2/authorize"
REGISTER_PATH = "/register"
LOGIN_PATH = "/authn/authenticate/IcaCustomers"
LISTS_PATH = "/sverige/digx/shopping-list/v1/api"

# Public values built into the ICA app. The app uses them to register itself
# with ICA's login server before each login; the same values are used by the
# open-source projects named above.
DCR_CLIENT_ID = "ica-app-dcr-registration"
DCR_CLIENT_SECRET = "uxLHTBvZ-Z2fV-SbrHl1E-tz7vB3jQFrwAdSLlbVMMu1rxDdvJU0s8KGu9d1wLS4"
DCR_SOFTWARE_ID = "dcr-ica-app-template"
REDIRECT_URI = "icacurity://app"
ACR = "urn:se:curity:authentication:html-form:IcaCustomers"

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

TIMEOUT = aiohttp.ClientTimeout(total=30)
EXPIRY_MARGIN = 60  # refresh this many seconds before the access token runs out
RELOGIN_PAUSE = 900  # at most one automatic password login per 15 minutes
REDIRECTS = (301, 302, 303, 307, 308)
MAX_HOPS = 6

TokenCallback = Callable[["IcaToken"], Awaitable[None]]
EventCallback = Callable[[str, str], None]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class IcaError(Exception):
    """Base class for everything this module raises."""


class IcaConnectionError(IcaError):
    """ICA couldn't be reached, timed out, or answered with a server error."""


class IcaGeoBlockedError(IcaConnectionError):
    """ICA refused the request because it didn't come from a Swedish IP (HTTP 451)."""


class IcaAuthError(IcaError):
    """ICA rejected the personnummer/password or the saved login.

    The password is needed again. Never retry this automatically: repeated
    failed logins can lock the ICA account.
    """


class IcaLoginFlowError(IcaError):
    """A login page answered something unexpected (ICA may have changed it)."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"login step '{step}': {detail}")
        self.step = step
        self.detail = detail


class IcaApiError(IcaError):
    """The shopping-list API refused a request."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def normalize_personnummer(value: str, today: date | None = None) -> str:
    """Return the personnummer as 12 digits (YYYYMMDDNNNN).

    ICA's login needs the century digits and answers HTTP 400 without them.
    Accepts 10 or 12 digits, with or without "-". A "+" in a 10-digit number
    means the person is 100 or older, as on Swedish ID documents.
    """
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        today = today or date.today()
        century = today.year // 100
        if int(digits[:2]) > today.year % 100:
            century -= 1
        if "+" in raw:
            century -= 1
        digits = f"{century:02d}{digits}"
    return digits


def personnummer_valid(pnr: str) -> bool:
    """True for 12 digits whose last digit is a correct check digit."""
    if len(pnr) != 12 or not pnr.isdigit():
        return False
    total = 0
    for index, char in enumerate(pnr[2:]):
        product = int(char) * (2 if index % 2 == 0 else 1)
        total += product - 9 if product > 9 else product
    return total % 10 == 0


def account_id(pnr: str) -> str:
    """A stable id for the account that doesn't reveal the personnummer."""
    return hashlib.sha256(f"ica_lists:{pnr}".encode()).hexdigest()[:20]


def mask_personnummer(pnr: str) -> str:
    """19900101-XXXX style: enough to recognise, not enough to misuse."""
    if len(pnr) != 12:
        return "…"
    return f"{pnr[:8]}-XXXX"


_SECRETISH = re.compile(r"\d{10,12}|[A-Za-z0-9_\-.=+/]{32,}")


def sanitize(text: Any, limit: int = 200) -> str:
    """Strip anything that looks like a personnummer or a token, and shorten."""
    cleaned = _SECRETISH.sub("…", str(text)).replace("\r", " ").replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def _pkce() -> tuple[str, str]:
    alphabet = string.ascii_letters + string.digits
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _query_param(url: str, key: str) -> str | None:
    """Read a parameter from a URL's query string or fragment."""
    if not url:
        return None
    parsed = urlparse(url)
    for part in (parsed.query, parsed.fragment):
        values = parse_qs(part).get(key)
        if values:
            return values[0]
    return None


def _expires_in(data: dict[str, Any]) -> float:
    try:
        return max(60.0, float(data.get("expires_in", 900)))
    except (TypeError, ValueError):
        return 900.0


def _name_from_id_token(id_token: Any) -> str | None:
    """The account holder's name from the id_token (not verified; display only)."""
    if not isinstance(id_token, str) or id_token.count(".") < 2:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    parts = [claims.get("given_name"), claims.get("family_name")]
    name = " ".join(str(p) for p in parts if p).strip()
    return name or (str(claims["name"]) if claims.get("name") else None)


class _Forms(HTMLParser):
    """Collect every form on a page with its action and inputs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "form":
            self._current = {
                "action": attributes.get("action", ""),
                "fields": {},
                "types": {},
            }
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None and attributes.get("name"):
            name = attributes["name"]
            self._current["fields"][name] = attributes.get("value", "")
            self._current["types"][name] = attributes.get("type", "text").lower()

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def _parse_forms(html: str) -> list[dict[str, Any]]:
    parser = _Forms()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001 - a broken page still gives us what was parsed
        _LOGGER.debug("Could not fully parse a login page", exc_info=True)
    return parser.forms


_TOKEN_FIELD = (
    re.compile(r'name=["\']token["\'][^>]*value=["\']([^"\']+)["\']', re.I),
    re.compile(r'value=["\']([^"\']+)["\'][^>]*name=["\']token["\']', re.I),
)


@dataclass
class _Page:
    """A response that wasn't a redirect."""

    status: int
    body: str
    url: str
    visited: list[str] = field(default_factory=list)


@dataclass
class _AppRedirect:
    """A redirect to the app's own URL scheme (where the login code arrives)."""

    location: str
    visited: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Tokens and login
# --------------------------------------------------------------------------
@dataclass
class IcaToken:
    """The tokens from a login, plus the client they were issued to."""

    access_token: str
    refresh_token: str
    expires_at: float
    client_id: str
    client_secret: str
    scope: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_MARGIN

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> IcaToken | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                expires_at=float(data["expires_at"]),
                client_id=str(data["client_id"]),
                client_secret=str(data["client_secret"]),
                scope=str(data.get("scope") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


class IcaAuth:
    """Logs in to ICA and keeps a valid access token around.

    One instance per account. Concurrent callers share one refresh.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        token: IcaToken | None = None,
        on_token: TokenCallback | None = None,
        on_event: EventCallback | None = None,
        credentials: tuple[str, str] | None = None,
        ims_base: str | None = None,
    ) -> None:
        self._session = session
        self._token = token
        self._on_token = on_token
        self._on_event = on_event
        self._credentials = credentials
        self._ims = (ims_base or IMS_BASE).rstrip("/")
        self._lock = asyncio.Lock()
        self._relogin_blocked = False
        self._relogin_not_before = 0.0
        self.account_name: str | None = None

    @property
    def token(self) -> IcaToken | None:
        return self._token

    def _url(self, path: str) -> str:
        return f"{self._ims}{path}"

    def _event(self, kind: str, message: str) -> None:
        _LOGGER.debug("%s: %s", kind, message)
        if self._on_event is not None:
            try:
                self._on_event(kind, sanitize(message))
            except Exception:  # noqa: BLE001 - logging must never break a login
                _LOGGER.debug("Event callback failed", exc_info=True)

    async def async_login(self, personnummer: str, password: str) -> IcaToken:
        """Log in with personnummer and password. One attempt, never retried."""
        async with self._lock:
            await self._login(normalize_personnummer(personnummer), password)
            assert self._token is not None
            return self._token

    async def async_access_token(self, force_refresh: bool = False) -> str:
        """A valid access token, refreshing (or logging in again) when needed."""
        async with self._lock:
            token = self._token
            if token is not None and not force_refresh and not token.expired:
                return token.access_token
            if token is not None:
                try:
                    await self._refresh(token)
                except IcaAuthError as err:
                    self._event("auth", f"refresh refused ({err}), need a new login")
                else:
                    assert self._token is not None
                    return self._token.access_token
            return await self._relogin()

    async def _relogin(self) -> str:
        if not self._credentials:
            raise IcaAuthError("The saved ICA login has expired. Sign in again.")
        if self._relogin_blocked:
            raise IcaAuthError("ICA rejected the saved password. Sign in again.")
        now = time.time()
        if now < self._relogin_not_before:
            raise IcaConnectionError("Waiting a while before logging in to ICA again")
        self._relogin_not_before = now + RELOGIN_PAUSE
        pnr, password = self._credentials
        try:
            await self._login(pnr, password)
        except IcaAuthError:
            self._relogin_blocked = True
            raise
        assert self._token is not None
        return self._token.access_token

    async def _refresh(self, token: IcaToken) -> None:
        basic = base64.b64encode(
            f"{token.client_id}:{token.client_secret}".encode()
        ).decode()
        data = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": token.refresh_token},
            step="refresh",
            headers={"Authorization": f"Basic {basic}"},
            auth_errors=True,
        )
        self._token = IcaToken(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or token.refresh_token),
            expires_at=time.time() + _expires_in(data),
            client_id=token.client_id,
            client_secret=token.client_secret,
            scope=str(data.get("scope") or token.scope),
        )
        _LOGGER.debug("ICA access token refreshed")
        await self._notify()

    async def _login(self, pnr: str, password: str) -> None:
        self._session.cookie_jar.clear()
        self._event("login", "logging in")

        # 1. A short token that is only allowed to register a client.
        boot = await self._token_request(
            {
                "client_id": DCR_CLIENT_ID,
                "client_secret": DCR_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": "dcr",
                "response_type": "token",
            },
            step="bootstrap",
        )

        # 2. Register a client for this login.
        page = await self._walk(
            "POST",
            self._url(REGISTER_PATH),
            step="register",
            json={"software_id": DCR_SOFTWARE_ID},
            headers={"Authorization": f"Bearer {boot['access_token']}"},
        )
        if not isinstance(page, _Page) or page.status not in (200, 201):
            raise IcaLoginFlowError("register", _describe(page))
        client = _json(page.body, "register")
        client_id = client.get("client_id")
        client_secret = client.get("client_secret")
        scope = str(client.get("scope") or "")
        if not client_id or not client_secret:
            raise IcaLoginFlowError("register", "no client in the answer")

        # 3. Start the authorization (PKCE) and open the login page.
        verifier, challenge = _pkce()
        result = await self._walk(
            "GET",
            self._url(AUTHORIZE_PATH),
            step="authorize",
            params={
                "client_id": client_id,
                "scope": scope,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
                "acr": ACR,
            },
        )
        if not isinstance(result, _Page) or result.status >= 400:
            raise IcaLoginFlowError("authorize", _describe(result))
        state = next(
            (s for s in (_query_param(u, "state") for u in result.visited) if s), None
        )

        # 4. Post the personnummer and password.
        action, fields = _login_form(result.body, result.url, self._url(LOGIN_PATH))
        answer = await self._walk("POST", action, step="login", data=fields | {
            _user_field(result.body): pnr,
            _password_field(result.body): password,
        })
        code = _code_from(answer)
        if code is None:
            if not isinstance(answer, _Page):
                raise IcaLoginFlowError("login", _describe(answer))
            if answer.status >= 500:
                raise IcaConnectionError(f"ICA's login answered HTTP {answer.status}")
            grant = _grant_form(answer.body, answer.url)
            if grant is None:
                # A refused login shows the form again (or answers 400).
                self._event("login", f"refused (HTTP {answer.status})")
                raise IcaAuthError("ICA did not accept that personnummer or password")
            grant_action, grant_fields = grant
            if state and "state" not in grant_fields:
                grant_fields["state"] = state
            if not grant_action:
                query = urlencode(
                    {"client_id": client_id, "forceAuthN": "true", "acr": ACR}
                )
                grant_action = f"{self._url(AUTHORIZE_PATH)}?{query}"

            # 5. Hand the login result to the authorization to get the code.
            done = await self._walk("POST", grant_action, step="code", data=grant_fields)
            code = _code_from(done)
            if code is None:
                raise IcaLoginFlowError("code", _describe(done))

        # 6. Swap the code for tokens.
        data = await self._token_request(
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "scope": scope,
                "response_type": "token",
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            step="token",
        )
        if not data.get("refresh_token"):
            raise IcaLoginFlowError("token", "no refresh_token in the answer")
        self._token = IcaToken(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=time.time() + _expires_in(data),
            client_id=str(client_id),
            client_secret=str(client_secret),
            scope=str(data.get("scope") or scope),
        )
        self.account_name = _name_from_id_token(data.get("id_token"))
        self._relogin_blocked = False
        self._session.cookie_jar.clear()
        self._event("login", "logged in")
        await self._notify()

    async def _notify(self) -> None:
        if self._on_token is not None and self._token is not None:
            await self._on_token(self._token)

    async def _token_request(
        self,
        form: dict[str, str],
        *,
        step: str,
        headers: dict[str, str] | None = None,
        auth_errors: bool = False,
    ) -> dict[str, Any]:
        page = await self._walk(
            "POST", self._url(TOKEN_PATH), step=step, data=form, headers=headers
        )
        if not isinstance(page, _Page):
            raise IcaLoginFlowError(step, _describe(page))
        if auth_errors and page.status in (400, 401):
            raise IcaAuthError(f"ICA refused the saved login (HTTP {page.status})")
        if page.status >= 500:
            raise IcaConnectionError(f"ICA's login server answered HTTP {page.status}")
        if page.status != 200:
            raise IcaLoginFlowError(step, _describe(page))
        data = _json(page.body, step)
        if not data.get("access_token"):
            raise IcaLoginFlowError(step, "no access_token in the answer")
        return data

    async def _walk(
        self,
        method: str,
        url: str,
        *,
        step: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> _Page | _AppRedirect:
        """Send a request and follow web redirects by hand.

        A redirect to the app's own scheme (icacurity://) ends the walk, since
        that is where ICA delivers the login code.
        """
        visited: list[str] = []
        for _ in range(MAX_HOPS):
            request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=request_headers,
                    allow_redirects=False,
                    timeout=TIMEOUT,
                    **kwargs,
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location", "")
                    final_url = str(resp.url)
                    body = await resp.text(errors="replace")
            except TimeoutError as err:
                raise IcaConnectionError(f"ICA timed out during '{step}'") from err
            except aiohttp.ClientError as err:
                raise IcaConnectionError(
                    f"Could not reach ICA during '{step}': {err}"
                ) from err
            if status == 451:
                raise IcaGeoBlockedError(
                    "ICA only answers requests from Swedish IP addresses (HTTP 451)"
                )
            if status not in REDIRECTS or not location:
                return _Page(status, body, final_url, visited)
            target = urljoin(final_url, location)
            visited.append(target)
            if urlparse(target).scheme not in ("http", "https"):
                return _AppRedirect(target, visited)
            if status in (301, 302, 303):
                method, kwargs = "GET", {}
            else:  # 307/308 keep the method and body; the query is in the target
                kwargs = {k: v for k, v in kwargs.items() if k != "params"}
            url, headers = target, None
        raise IcaLoginFlowError(step, "too many redirects")


def _describe(result: _Page | _AppRedirect | None) -> str:
    if isinstance(result, _Page):
        return f"HTTP {result.status}"
    if isinstance(result, _AppRedirect):
        error = _query_param(result.location, "error")
        return f"returned to the app without a code ({error or 'no error given'})"
    return "no answer"


def _json(body: str, step: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError as err:
        raise IcaLoginFlowError(step, "the answer was not JSON") from err
    if not isinstance(data, dict):
        raise IcaLoginFlowError(step, "unexpected answer")
    return data


def _code_from(result: _Page | _AppRedirect) -> str | None:
    if isinstance(result, _AppRedirect):
        return _query_param(result.location, "code")
    return None


def _password_form(html: str) -> dict[str, Any] | None:
    for form in _parse_forms(html):
        if "password" in form["types"].values():
            return form
    return None


def _login_form(html: str, page_url: str, default_action: str) -> tuple[str, dict[str, str]]:
    """Where to post the credentials, and the form's hidden fields."""
    form = _password_form(html)
    if form is None:
        return default_action, {}
    hidden = {
        name: value
        for name, value in form["fields"].items()
        if form["types"].get(name) == "hidden"
    }
    action = urljoin(page_url, form["action"]) if form["action"] else page_url
    if urlparse(action).path != urlparse(default_action).path:
        action = default_action
    return action, hidden


def _user_field(html: str) -> str:
    form = _password_form(html)
    if form is None or "userName" in form["fields"]:
        return "userName"
    candidates = [
        name
        for name, kind in form["types"].items()
        if kind in ("text", "tel", "email", "number")
    ]
    return candidates[0] if len(candidates) == 1 else "userName"


def _password_field(html: str) -> str:
    form = _password_form(html)
    if form is None:
        return "password"
    for name, kind in form["types"].items():
        if kind == "password":
            return name
    return "password"


def _grant_form(html: str, page_url: str) -> tuple[str, dict[str, str]] | None:
    """The auto-submitting form ICA shows after a successful login."""
    for form in _parse_forms(html):
        if form["fields"].get("token"):
            action = urljoin(page_url, form["action"]) if form["action"] else ""
            if action and urlparse(action).path != AUTHORIZE_PATH:
                action = ""
            return action, dict(form["fields"])
    for pattern in _TOKEN_FIELD:
        if match := pattern.search(html or ""):
            return "", {"token": match.group(1)}
    return None


# --------------------------------------------------------------------------
# Shopping lists
# --------------------------------------------------------------------------
@dataclass
class IcaRow:
    """One item on an ICA list. `raw` is ICA's row exactly as sent."""

    id: str
    text: str
    striked: bool
    raw: dict[str, Any] = field(default_factory=dict, compare=True)

    @property
    def quantity_text(self) -> str | None:
        quantity = self.raw.get("quantity")
        if not isinstance(quantity, dict):
            return None
        amount = quantity.get("amount")
        unit = str(quantity.get("unit") or "").strip()
        if amount in (None, "", 0):
            return None
        try:
            amount_text = f"{float(amount):g}"
        except (TypeError, ValueError):
            amount_text = str(amount)
        return f"{amount_text} {unit}".strip()


@dataclass
class IcaList:
    """One ICA shopping list with its rows."""

    id: str
    name: str
    rows: list[IcaRow] = field(default_factory=list)

    def row(self, row_id: str | None) -> IcaRow | None:
        return next((row for row in self.rows if row.id == row_id), None)


def _row(data: dict[str, Any]) -> IcaRow:
    return IcaRow(
        id=str(data["id"]),
        text=str(data.get("text") or ""),
        striked=bool(data.get("isStriked")),
        raw=dict(data),
    )


def _label(path: str) -> str:
    """The endpoint without list or row ids, for messages."""
    if path.startswith("/list/") and path != "/list/all":
        return "/list/{id}/row"
    if path.startswith("/row/"):
        return "/row/{id}"
    return path


class IcaClient:
    """Reads and changes ICA shopping lists."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: IcaAuth,
        *,
        api_base: str | None = None,
    ) -> None:
        self._session = session
        self._auth = auth
        self._base = (api_base or API_BASE).rstrip("/")

    @property
    def auth(self) -> IcaAuth:
        return self._auth

    async def async_get_lists(self) -> list[IcaList]:
        data = await self._api("GET", "/list/all")
        lists: list[IcaList] = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            rows = [
                _row(row)
                for row in entry.get("rows") or []
                if isinstance(row, dict) and row.get("id")
            ]
            lists.append(
                IcaList(id=str(entry["id"]), name=str(entry.get("name") or "ICA"), rows=rows)
            )
        return lists

    async def async_add_row(self, list_id: str, text: str) -> IcaRow | None:
        """Add an item. ICA puts free text into a category by itself."""
        data = await self._api(
            "POST",
            f"/list/{list_id}/row",
            payload={"isStriked": False, "quantity": {}, "text": text, "article": None},
        )
        if isinstance(data, dict) and data.get("id"):
            return _row(data)
        return None

    async def async_update_row(
        self, row: IcaRow, *, text: str | None = None, striked: bool | None = None
    ) -> IcaRow:
        """Change an item's text and/or ticked state.

        ICA replaces the whole row, so the full original row is sent back with
        only these fields changed. Anything less would wipe the quantity and
        category someone set in the ICA app.
        """
        body = dict(row.raw)
        body["id"] = row.id
        if text is not None:
            body["text"] = text
        if striked is not None:
            body["isStriked"] = striked
        data = await self._api("PUT", f"/row/{row.id}", payload=body)
        if isinstance(data, dict) and data.get("id"):
            return _row(data)
        return IcaRow(
            id=row.id,
            text=str(body.get("text") or ""),
            striked=bool(body.get("isStriked")),
            raw=body,
        )

    async def async_delete_row(self, row_id: str) -> None:
        """Remove an item. An item that is already gone counts as removed."""
        await self._api("DELETE", f"/row/{row_id}", allow_404=True)

    async def _api(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        allow_404: bool = False,
        retry: bool = True,
    ) -> Any:
        token = await self._auth.async_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        label = _label(path)
        try:
            async with self._session.request(
                method,
                f"{self._base}{LISTS_PATH}{path}",
                json=payload,
                headers=headers,
                timeout=TIMEOUT,
            ) as resp:
                status = resp.status
                body = await resp.text(errors="replace")
        except TimeoutError as err:
            raise IcaConnectionError(f"ICA timed out on {method} {label}") from err
        except aiohttp.ClientError as err:
            raise IcaConnectionError(f"Could not reach ICA ({method} {label}): {err}") from err

        if status in (401, 403) and retry:
            # The token can die before its time; get a fresh one and try once more.
            await self._auth.async_access_token(force_refresh=True)
            return await self._api(
                method, path, payload=payload, allow_404=allow_404, retry=False
            )
        if status == 401:
            raise IcaAuthError("ICA refused the login token")
        if status == 451:
            raise IcaGeoBlockedError(
                "ICA only answers requests from Swedish IP addresses (HTTP 451)"
            )
        if status == 404 and allow_404:
            return None
        if status >= 500:
            raise IcaConnectionError(f"ICA answered HTTP {status} on {method} {label}")
        if status >= 400:
            raise IcaApiError(
                status, f"ICA answered HTTP {status} on {method} {label}: {sanitize(body, 120)}"
            )
        body = body.strip()
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError as err:
            raise IcaApiError(
                status, f"ICA answered {method} {label} with something that isn't JSON"
            ) from err
