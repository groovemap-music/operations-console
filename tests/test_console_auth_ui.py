"""Browser coverage for the console's authentication boundary.

An anonymous browser gets the login form and ``/health`` and nothing else; a signed-in admin
gets the Overview first and the admin tabs after it; and each way a session ends (signing out,
expiring mid-use, being revoked elsewhere, catalog-api becoming unreachable) returns the browser
to the login form with a visible reason. The bearer lives only in an HttpOnly cookie, never in
storage page script can read, and a cross-site page cannot use that cookie to change anything.
The fake catalog-api runs as its own server, so every session check crosses a real socket.
"""
# mypy: disable-error-code="no-untyped-def"

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_console import CONSOLE_HEADERS, CONSOLE_URL, DATA_ROUTES, SECURE_COOKIE, WS_URL, fake_catalog, session_cookie, sign_in
from tests.fake_catalog_api import ADMIN_EMAIL, ADMIN_PASSWORD, SHORT_LIVED_EMAIL


if TYPE_CHECKING:
    from collections.abc import Iterator


ADMIN_TABS = [
    "Extractions",
    "DLQ Management",
    "User Activity",
    "Storage",
    "Queue Trends",
    "System Health",
    "Audit Log",
    "Extraction Analysis",
    "Ledger",
]

# Opens a WebSocket from the page and resolves with "message" on the first frame or the close code.
_OPEN_FEED = """(url) => new Promise((resolve) => {
    const ws = new WebSocket(url);
    ws.onmessage = () => { ws.onclose = null; ws.close(1000); resolve('message'); };
    ws.onclose = (event) => resolve(event.code);
})"""

# Everything page script can read that could hold a credential.
_SCRIPT_READABLE = """() => JSON.stringify({
    local: Object.fromEntries(Object.entries(localStorage)),
    session: Object.fromEntries(Object.entries(sessionStorage)),
    cookie: document.cookie,
})"""

ATTACKER = "http://attacker.test"
# Same site as the console (another port on localhost), so SameSite=Strict lets the browser send
# the cookie; in development it is Grafana's address, which CORS_ORIGINS used to trust.
SAME_SITE = "http://localhost:3000"
# A cross-site page that submits a form to the console the moment it loads. text/plain lets it
# post a JSON-looking body, the classic way around a JSON-only endpoint.
_FORGED_TRIGGER = f"""<!doctype html><html><body>
<form id="forged" method="post" enctype="text/plain" action="{CONSOLE_URL}/admin/api/extractions/trigger">
<input type="hidden" name='{{"source":"discogs","pad":"' value='"}}'>
</form>
<script>document.getElementById('forged').submit();</script>
</body></html>"""


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
        # The page cannot see its HttpOnly cookie, so it asks once whether it holds a session.
        assert [url for url in console_requests if re.search(r"/(api|admin/api|ws)\b", url)] == [f"{CONSOLE_URL}/api/session"], console_requests

        for path in DATA_ROUTES:
            response = page.request.get(f"{CONSOLE_URL}{path}")
            assert response.status == 401, path
            assert response.json()["reason"] == "missing", path
        assert page.request.post(f"{CONSOLE_URL}/api/ws-ticket", headers=CONSOLE_HEADERS).status == 401
        assert page.request.get(f"{CONSOLE_URL}/health").status == 200
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert page.request.get(f"{CONSOLE_URL}{path}").status == 404, path

    @pytest.mark.parametrize("query", ["", "?ticket=forged"])
    def test_an_anonymous_websocket_is_closed_with_4401(self, page: Page, query: str) -> None:
        page.goto(CONSOLE_URL, wait_until="domcontentloaded")
        assert page.evaluate(_OPEN_FEED, f"{WS_URL}/ws{query}") == 4401

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
        # The context's request API carries the browser's session cookie, as the page's fetch does.
        response = page.request.post(f"{CONSOLE_URL}/api/ws-ticket", headers=CONSOLE_HEADERS)
        assert response.ok
        url = f"{WS_URL}/ws?ticket={response.json()['ticket']}"

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
        assert session_cookie(page) is None, "signing out clears the session cookie"
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
        assert session_cookie(page) is None, "the browser drops the cookie at the session's expiry"

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


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestBearerStaysOutOfScript:
    def test_no_script_readable_storage_holds_the_bearer(self, page: Page) -> None:
        sign_in(page)
        expect(page.locator(".status-text")).to_have_text("Connected", timeout=15000)

        cookie = session_cookie(page)
        assert cookie is not None and cookie["value"], "the session lives in the cookie"
        assert cookie["httpOnly"] is True
        assert cookie["sameSite"] == "Strict"
        assert cookie["path"] == "/"
        assert cookie["secure"] is SECURE_COOKIE, "Secure by default; only the WebKit projects opt out"
        bearer = cookie["value"]
        readable = page.evaluate(_SCRIPT_READABLE)
        assert bearer not in readable
        assert cookie["name"] not in readable

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#console-view")).to_be_visible(timeout=10000)
        assert bearer not in page.evaluate(_SCRIPT_READABLE)

    def test_the_login_answer_carries_no_bearer(self, page: Page) -> None:
        # Through the context's request API, which shares the browser's cookie jar: Chromium does
        # not reliably hand back the body of a response the page itself received.
        answer = page.request.post(f"{CONSOLE_URL}/admin/api/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=CONSOLE_HEADERS)
        assert answer.ok
        assert answer.json() == {"expires_in": 3600}
        cookie = session_cookie(page)
        assert cookie is not None
        assert cookie["value"] not in answer.text()

    def test_a_bearer_an_older_console_stored_is_signed_out_and_removed_on_load(self, page: Page) -> None:
        legacy = fake_catalog("mint")["token"]
        page.goto(CONSOLE_URL, wait_until="domcontentloaded")
        page.evaluate("(token) => { localStorage.setItem('admin_token', token); localStorage.setItem('admin_email', 'admin@example.com'); }", legacy)

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#login-view")).to_be_visible(timeout=10000)
        expect(page.locator("#login-reason")).to_be_hidden()
        assert legacy not in page.evaluate(_SCRIPT_READABLE)
        assert page.evaluate("() => localStorage.getItem('admin_email')") is None
        retired = page.request.get(f"{CONSOLE_URL}/api/session", headers={"Authorization": f"Bearer {legacy}"})
        assert retired.status == 401, "the stored bearer was signed out at catalog-api, not just forgotten"

    @pytest.mark.parametrize("origin", [ATTACKER, SAME_SITE], ids=["cross-site", "same-site"])
    def test_a_form_on_another_origin_cannot_trigger_an_extraction(self, page: Page, origin: str) -> None:
        sign_in(page)
        page.route(f"{origin}/**", lambda route: route.fulfill(status=200, content_type="text/html", body=_FORGED_TRIGGER))

        with page.expect_response(f"{CONSOLE_URL}/admin/api/extractions/trigger") as forged:
            page.goto(f"{origin}/", wait_until="commit")
        assert forged.value.status == 403
        # The refusal is read from the rendered page, not the response: Chromium does not
        # reliably hand back the body of a response the page itself received.
        page.wait_for_url(f"{CONSOLE_URL}/admin/api/extractions/trigger")
        expect(page.locator("body")).to_contain_text("Cross-site request refused")
        assert page.request.get(f"{CONSOLE_URL}/api/session").ok, "the forged request did not end the session"

    def test_a_request_riding_the_cookie_without_the_console_header_is_refused(self, page: Page) -> None:
        """What a browser that ignored SameSite would send: the cookie, but no console header."""
        sign_in(page)
        for path in (
            "/admin/api/dlq/purge/groovemap-discogs-graphinator-artists.dlq",
            "/admin/api/extractions/trigger",
            "/api/ws-ticket",
            "/admin/api/logout",
        ):
            response = page.request.post(f"{CONSOLE_URL}{path}", headers={"Origin": ATTACKER, "Sec-Fetch-Site": "cross-site"})
            assert response.status == 403, path
        assert page.request.get(f"{CONSOLE_URL}/api/session").ok
