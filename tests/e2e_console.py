"""Shared browser-suite helpers: where the console and its fake catalog-api listen, and sign-in."""

import os
import time
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.fake_catalog_api import ADMIN_EMAIL, ADMIN_PASSWORD


CONSOLE_URL = "http://localhost:8003"
CATALOG_URL = "http://127.0.0.1:18004"
DATA_ROUTES = ["/api/metrics", "/api/services", "/api/queues", "/api/databases", "/api/queue-prefixes", "/api/topology", "/api/session"]
# The console's session cookie is Secure by default, which Chromium and Firefox accept from
# http://localhost. WebKit refuses a Secure cookie there, so only the WebKit projects run the
# console with the documented opt-out, SESSION_COOKIE_SECURE=false, and its plain cookie name.
SECURE_COOKIE = os.environ.get("GROOVEMAP_E2E_PROJECT", "chromium") not in {"webkit", "iphone", "ipad"}
SESSION_COOKIE = "__Host-groovemap_console_session" if SECURE_COOKIE else "groovemap_console_session"
# The header the console's own pages add so a state-changing request is not taken for a
# cross-site one.
CONSOLE_HEADERS = {"X-GrooveMap-Console": "1"}


def sign_in(page: Page, email: str = ADMIN_EMAIL) -> None:
    """Open the console and sign in through the login form; wait for the console to show."""
    page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=10000)
    expect(page.locator("#login-view")).to_be_visible(timeout=10000)
    page.fill("#login-email", email)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.click("#login-btn")
    expect(page.locator("#console-view")).to_be_visible(timeout=10000)


def freeze_live_feed(page: Page) -> None:
    """Stop every live reading overwriting payloads a test injects: the socket's and the first HTTP one.

    The socket is opened after sign-in (it first trades the session for a ticket), so a handler
    cleared before it exists either fails or leaves the first live frame to replace the payload.
    The first reading also arrives over ``/api/metrics``, independently of the socket; applied
    after an injected scenario it drops every queue the live feed does not report from the
    history, so the scenario's cells read "not reported". Both are settled before this returns.
    """
    page.wait_for_function("() => window.dashboard.ws && window.dashboard.ws.readyState === WebSocket.OPEN", timeout=10000)
    page.evaluate("() => { window.dashboard.ws.onmessage = null; return window.dashboard.initialLoad; }")


def fake_catalog(path: str, body: dict[str, object] | None = None) -> Any:
    """Drive the standalone fake catalog-api's test controls; return what it answered."""
    response = httpx.post(f"{CATALOG_URL}/__fake__/{path}", json=body or {}, timeout=5.0)
    response.raise_for_status()
    return response.json()


def session_cookie(page: Page) -> dict[str, Any] | None:
    """The browser's live session cookie for the console, attributes included, or None.

    A browser may still list a cookie past its Max-Age until it purges it, but never sends it
    again, so an expired one counts as gone.
    """
    return next(
        (
            cookie
            for cookie in page.context.cookies(CONSOLE_URL)
            if cookie["name"] == SESSION_COOKIE and (cookie["expires"] < 0 or cookie["expires"] > time.time())
        ),
        None,
    )
