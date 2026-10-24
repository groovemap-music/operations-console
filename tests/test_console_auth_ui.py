"""Browser coverage for the console's authentication boundary.

An anonymous browser gets the login form and ``/health`` and nothing else; a signed-in admin
gets the Overview first and the admin tabs after it; and each way a session ends (signing out,
expiring mid-use, being revoked elsewhere, catalog-api becoming unreachable) returns the browser
to the login form with a visible reason. The fake catalog-api runs as its own server, so every
session check crosses a real socket.
"""
# mypy: disable-error-code="no-untyped-def"

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_console import CONSOLE_URL, DATA_ROUTES, fake_catalog, sign_in
from tests.fake_catalog_api import SHORT_LIVED_EMAIL


if TYPE_CHECKING:
    from collections.abc import Iterator


ADMIN_TABS = ["Extractions", "DLQ Management", "User Activity", "Storage", "Queue Trends", "System Health", "Audit Log", "Extraction Analysis"]

# Opens a WebSocket from the page and resolves with "message" on the first frame or the close code.
_OPEN_FEED = """(url) => new Promise((resolve) => {
    const ws = new WebSocket(url);
    ws.onmessage = () => { ws.onclose = null; ws.close(1000); resolve('message'); };
    ws.onclose = (event) => resolve(event.code);
})"""


@pytest.fixture(autouse=True)
def _catalog_reachable(test_server: None) -> Iterator[None]:
    fake_catalog("outage", {"enabled": False})
    yield
    fake_catalog("outage", {"enabled": False})


def _reason(page: Page) -> str:
    return page.locator("#login-reason").get_attribute("data-reason") or ""


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestAnonymousBrowser:
    def test_anonymous_browser_reads_nothing_but_health_and_the_login_form(self, page: Page) -> None:
        requested: list[str] = []
        page.on("request", lambda request: requested.append(request.url))

        page.goto(CONSOLE_URL, wait_until="load")
        expect(page.locator("#login-view")).to_be_visible(timeout=10000)
        page.wait_for_timeout(1000)

        expect(page.locator("#console-view")).to_be_hidden()
        expect(page.locator("#login-email")).to_be_visible()
        expect(page.locator("#login-password")).to_be_visible()
        expect(page.locator("#login-reason")).to_be_hidden()
        console_requests = [url for url in requested if url.startswith(CONSOLE_URL)]
        assert not [url for url in console_requests if re.search(r"/(api|admin/api|ws)\b", url)], console_requests

        for path in DATA_ROUTES:
            response = page.request.get(f"{CONSOLE_URL}{path}")
            assert response.status == 401, path
            assert response.json()["reason"] == "missing", path
        assert page.request.post(f"{CONSOLE_URL}/api/ws-ticket").status == 401
        assert page.request.get(f"{CONSOLE_URL}/health").status == 200

    @pytest.mark.parametrize("query", ["", "?ticket=forged"])
    def test_an_anonymous_websocket_is_closed_with_4401(self, page: Page, query: str) -> None:
        page.goto(CONSOLE_URL, wait_until="domcontentloaded")
        assert page.evaluate(_OPEN_FEED, f"ws://localhost:8003/ws{query}") == 4401

    @pytest.mark.parametrize("path", ["/admin", "/admin.html"])
    def test_the_old_admin_page_redirects_to_the_shell(self, page: Page, path: str) -> None:
        page.goto(f"{CONSOLE_URL}{path}", wait_until="domcontentloaded")
        expect(page).to_have_url(f"{CONSOLE_URL}/")
        expect(page.locator("#login-view")).to_be_visible(timeout=10000)


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestSignedInShell:
    def test_overview_is_first_and_the_admin_tabs_follow(self, page: Page) -> None:
        sign_in(page)

        tabs = page.locator("nav .tab-btn")
        expect(tabs).to_have_count(1 + len(ADMIN_TABS))
        expect(tabs.first).to_contain_text("Overview")
        expect(tabs.first).to_have_class(re.compile(r"\bactive\b"))
        for index, label in enumerate(ADMIN_TABS, start=1):
            expect(tabs.nth(index)).to_contain_text(label)
        expect(page.locator("#tab-overview")).to_be_visible()
        expect(page.locator("#service-extractor-discogs")).to_be_visible(timeout=10000)
        expect(page.locator(".status-text")).to_have_text("Connected", timeout=15000)

        page.click("#tab-btn-extractions")
        expect(page.locator("#tab-extractions")).to_be_visible()
        expect(page.locator("#tab-overview")).to_be_hidden()
        page.click("#tab-btn-overview")
        expect(page.locator("#tab-overview")).to_be_visible()

    def test_a_reload_resumes_a_live_session(self, page: Page) -> None:
        sign_in(page)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#console-view")).to_be_visible(timeout=10000)
        expect(page.locator("#login-view")).to_be_hidden()

    def test_a_websocket_ticket_opens_exactly_one_socket(self, page: Page) -> None:
        sign_in(page)
        token = page.evaluate("() => localStorage.getItem('admin_token')")
        response = page.request.post(f"{CONSOLE_URL}/api/ws-ticket", headers={"Authorization": f"Bearer {token}"})
        assert response.ok
        url = f"ws://localhost:8003/ws?ticket={response.json()['ticket']}"

        assert page.evaluate(_OPEN_FEED, url) == "message"
        assert page.evaluate(_OPEN_FEED, url) == 4401


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestSessionEndings:
    def test_signing_out_returns_to_the_login_form_with_a_reason(self, page: Page) -> None:
        sign_in(page)
        page.click("#logout-btn")

        expect(page.locator("#login-view")).to_be_visible()
        expect(page.locator("#console-view")).to_be_hidden()
        expect(page.locator("#login-reason")).to_have_text("You signed out.")
        assert page.evaluate("() => localStorage.getItem('admin_token')") is None
        expect(page.locator(".status-text")).to_have_text("Disconnected")

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#login-view")).to_be_visible(timeout=10000)
        expect(page.locator("#console-view")).to_be_hidden()

    def test_a_session_that_expires_mid_use_returns_to_the_login_form(self, page: Page) -> None:
        sign_in(page, SHORT_LIVED_EMAIL)
        expect(page.locator("#tab-overview")).to_be_visible()

        expect(page.locator("#login-view")).to_be_visible(timeout=15000)
        expect(page.locator("#console-view")).to_be_hidden()
        expect(page.locator("#login-reason")).to_have_text("Your session expired or was revoked. Sign in again.")
        assert _reason(page) == "invalid"

    def test_a_session_revoked_elsewhere_returns_to_the_login_form(self, page: Page) -> None:
        page.clock.install()
        sign_in(page)

        fake_catalog("revoke-all")
        page.clock.fast_forward("00:16")

        expect(page.locator("#login-view")).to_be_visible(timeout=10000)
        assert _reason(page) == "invalid"

    def test_an_unreachable_catalog_api_returns_to_the_login_form(self, page: Page) -> None:
        page.clock.install()
        sign_in(page)

        fake_catalog("outage", {"enabled": True})
        page.clock.fast_forward("00:16")

        expect(page.locator("#login-view")).to_be_visible(timeout=10000)
        expect(page.locator("#login-reason")).to_have_text(
            "catalog-api is unreachable, so your session could not be verified. Sign in again once it is back."
        )
        assert _reason(page) == "unavailable"

        # The bearer survives an outage, so a reload after recovery resumes the session.
        fake_catalog("outage", {"enabled": False})
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#console-view")).to_be_visible(timeout=10000)
