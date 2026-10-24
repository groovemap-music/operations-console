"""Shared browser-suite helpers: where the console and its fake catalog-api listen, and sign-in."""

import httpx
from playwright.sync_api import Page, expect

from tests.fake_catalog_api import ADMIN_EMAIL, ADMIN_PASSWORD


CONSOLE_URL = "http://localhost:8003"
CATALOG_URL = "http://127.0.0.1:18004"
DATA_ROUTES = ["/api/metrics", "/api/services", "/api/queues", "/api/databases", "/api/queue-prefixes", "/api/session"]


def sign_in(page: Page, email: str = ADMIN_EMAIL) -> None:
    """Open the console and sign in through the login form; wait for the console to show."""
    page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=10000)
    expect(page.locator("#login-view")).to_be_visible(timeout=10000)
    page.fill("#login-email", email)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.click("#login-btn")
    expect(page.locator("#console-view")).to_be_visible(timeout=10000)


def fake_catalog(path: str, body: dict[str, object] | None = None) -> None:
    """Drive the standalone fake catalog-api's test controls."""
    httpx.post(f"{CATALOG_URL}/__fake__/{path}", json=body or {}, timeout=5.0).raise_for_status()
