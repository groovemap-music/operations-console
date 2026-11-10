"""Browser coverage for the end-of-load wait: a parked extraction_complete is not a critical NO CONSUMER.

At the end of a load a type's consumer is cancelled and its extraction_complete is queued for a few
seconds. The loader lists the type in ``awaiting_extraction_complete`` on /health, and the console
must show that wait quietly, while a queue nobody excuses stays critical. This runs the console's
real EventDetector, aggregation, WebSocket and front end in a real browser, over a source where both
Discogs consumers of ``artists`` are cancelled with a message queued: Graphinator lists the type,
Tableinator is a service version that does not report the field. The wait does not run on the clock: the test ages it past the production bound on demand, so
it cannot race how long the browser took to start or log in.
"""
# mypy: disable-error-code="no-untyped-def"

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

from tests.conftest import _server_log, _start_server, _stop_server, _wait_until_ready
from tests.e2e_console import free_port
from tests.fake_catalog_api import ADMIN_EMAIL, ADMIN_PASSWORD


if TYPE_CHECKING:
    from collections.abc import Iterator


GRAPHINATOR_ARTISTS = "#discogs-graphinator-artists-cell"
TABLEINATOR_ARTISTS = "#discogs-tableinator-artists-cell"


@pytest.fixture(scope="module")
def parked_marker_console() -> Iterator[str]:
    """A console over a source at the end of a load, with its own fake catalog-api, on their own ports."""
    root = str(Path(__file__).resolve().parents[1])
    catalog_port, console_port = free_port(), free_port()
    token = uuid.uuid4().hex
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "API_HOST": "127.0.0.1",
        "API_PORT": str(catalog_port),
        "SESSION_CACHE_SECONDS": "0",
        "E2E_RUN_TOKEN": token,
        "SESSION_COOKIE_SECURE": "true" if os.environ.get("GROOVEMAP_E2E_PROJECT", "chromium") not in {"webkit", "iphone", "ipad"} else "false",
    }
    catalog = _start_server("tests.fake_catalog_api:create_app", catalog_port, env, root)
    try:
        _wait_until_ready(catalog, f"http://127.0.0.1:{catalog_port}/__fake__/health", root, _server_log(root, catalog_port), token)
        console = _start_server("tests.dashboard_test_app:create_test_app_with_parked_marker", console_port, env, root)
        try:
            _wait_until_ready(console, f"http://localhost:{console_port}/health", root, _server_log(root, console_port), token)
            yield f"http://localhost:{console_port}"
        finally:
            _stop_server(console)
    finally:
        _stop_server(catalog)


def _open(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=10000)
    expect(page.locator("#login-view")).to_be_visible(timeout=10000)
    page.fill("#login-email", ADMIN_EMAIL)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.click("#login-btn")
    expect(page.locator("#console-view")).to_be_visible(timeout=10000)


@pytest.mark.e2e
class TestAwaitingExtractionComplete:
    def test_a_listed_wait_is_quiet_an_unlisted_one_is_critical_and_the_wait_lapses(self, page: Page, parked_marker_console: str) -> None:
        _open(page, parked_marker_console)
        graphinator, tableinator = page.locator(GRAPHINATOR_ARTISTS), page.locator(TABLEINATOR_ARTISTS)

        # Tableinator does not report the field: a real fault, critical, with an attention item.
        expect(tableinator).to_have_attribute("data-state", "crit", timeout=10000)
        expect(tableinator).to_have_attribute("data-kind", "no-consumer")
        expect(page.locator('[data-attention-id="queue:groovemap-discogs-tableinator-artists"]')).to_be_visible()

        # Graphinator lists the type: an expected wait, not critical and not on the attention bar.
        # The wait is 60 s from the first reading and this test ages it by hand, so it is quiet however slow the page was.
        expect(graphinator).to_have_attribute("data-state", "ok")
        expect(graphinator).to_have_attribute("data-kind", "awaiting-marker")
        expect(graphinator).to_contain_text("awaiting extraction_complete")
        expect(graphinator).not_to_contain_text("NO CONSUMER")
        expect(page.locator('[data-attention-id="queue:groovemap-discogs-graphinator-artists"]')).to_have_count(0)

        # Still waiting after the bound (the test moves the wait's start back by it): visible as a problem again.
        aged = page.request.post(f"{parked_marker_console}/__fake__/age-awaiting-marker")
        assert aged.ok
        assert aged.json() == {"aged": 1}
        expect(graphinator).to_have_attribute("data-state", "crit", timeout=10000)
        expect(graphinator).to_have_attribute("data-kind", "no-consumer")
        expect(graphinator).to_contain_text("NO CONSUMER")
        expect(page.locator('[data-attention-id="queue:groovemap-discogs-graphinator-artists"]')).to_be_visible()
