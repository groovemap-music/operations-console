"""Browser coverage for the Ledger tab (design ADR 0016 section 7, concept D).

The tab reads the three Identity Ledger routes through the console's proxy from the standalone
fake catalog-api, so every read crosses a real socket. It reads nothing until it is shown, and
its density strip is sized when its tab is, not while hidden. At phone width nothing spills out
of the page. Backfilled events say they are dated from the MusicBrainz full export. Every view
says when the ledger was last refreshed, and before its first refresh the tab says the ledger is
being built, then reads it again once catalog-api says to.
"""
# mypy: disable-error-code="no-untyped-def"

from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_console import fake_catalog, sign_in
from tests.fake_catalog_api import LEDGER_EDITION, LEDGER_OTHER_WORK, LEDGER_SPLIT_EDITION, LEDGER_WORK


if TYPE_CHECKING:
    from collections.abc import Iterator


PHONE = {"width": 390, "height": 844}
LEDGER_PATH = "/admin/api/identity/ledger/"

# The chart's drawn size against the strip that holds it.
STRIP_SIZE = """
() => {
  const chart = window.adminDashboard.state.ledgerChart;
  const strip = document.querySelector('#tab-ledger .ledger-strip').getBoundingClientRect();
  return { chart: chart ? [chart.width, chart.height] : null, strip: [strip.width, strip.height] };
}
"""

# How far the page scrolls sideways, and which Ledger parts spill out of their box or the viewport.
LAYOUT_FAULTS = """
() => {
  const page = document.documentElement;
  const panel = document.getElementById('tab-ledger');
  const right = panel.getBoundingClientRect().right;
  const spills = [...panel.querySelectorAll('.ledger-head, .ledger-controls > *, .ledger-strip, .ledger-edition, .ledger-fields dd, .ledger-events li')]
    .filter((el) => el.offsetParent !== null && (el.scrollWidth > el.clientWidth + 1 || el.getBoundingClientRect().right > right + 1))
    .map((el) => el.id || el.className || el.tagName);
  const table = panel.querySelector('.ledger-table');
  return { overflow: Math.max(0, page.scrollWidth - page.clientWidth), spills, tableScrolls: table.scrollWidth > table.clientWidth, tableRight: table.getBoundingClientRect().right - right };
}
"""


@pytest.fixture(autouse=True)
def _default_ledger(test_server: None) -> Iterator[None]:
    fake_catalog("ledger", {})
    fake_catalog("ledger-building", {"enabled": False})
    yield
    fake_catalog("ledger", {})
    fake_catalog("ledger-building", {"enabled": False})


def _record_ledger_reads(page: Page) -> list[str]:
    reads: list[str] = []
    page.on("request", lambda request: reads.append(request.url) if LEDGER_PATH in request.url else None)
    return reads


def _wait_for_drawn_strip(page: Page) -> dict[str, list[float]]:
    assert page.evaluate("() => typeof Chart") == "function", "Chart.js did not load from its CDN"
    page.wait_for_function(
        """() => {
          const chart = window.adminDashboard.state.ledgerChart;
          const strip = document.querySelector('#tab-ledger .ledger-strip').getBoundingClientRect();
          return chart && chart.width > 0 && Math.abs(chart.width - strip.width) <= 2;
        }""",
        timeout=10000,
    )
    size: dict[str, list[float]] = page.evaluate(STRIP_SIZE)
    return size


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestLedgerTab:
    def test_the_tab_reads_nothing_until_shown_and_sizes_its_strip_when_it_is(self, page: Page) -> None:
        reads = _record_ledger_reads(page)
        sign_in(page)
        expect(page.locator("#tab-ledger")).to_be_hidden()
        page.wait_for_timeout(500)
        assert reads == []

        page.click("#tab-btn-ledger")
        expect(page.locator("#tab-ledger")).to_be_visible()
        expect(page.locator("#tab-overview")).to_be_hidden()
        expect(page.locator("#ledger-works-body tr[data-work]")).to_have_count(2)
        size = _wait_for_drawn_strip(page)
        assert size["chart"] is not None
        assert size["chart"][1] == pytest.approx(size["strip"][1], abs=2)
        assert size["strip"][1] > 100
        assert sorted(urlparse(url).path for url in reads) == ["/admin/api/identity/ledger/density", "/admin/api/identity/ledger/works"]

        # Hidden again while the window changes width: shown again, it is redrawn at the new
        # width without re-reading the ledger.
        viewport = page.viewport_size
        assert viewport is not None
        page.click("#tab-btn-overview")
        expect(page.locator("#tab-ledger")).to_be_hidden()
        page.set_viewport_size({"width": max(360, viewport["width"] - 200), "height": viewport["height"]})
        page.click("#tab-btn-ledger")
        resized = _wait_for_drawn_strip(page)
        assert resized["chart"] is not None
        assert resized["chart"][0] < size["chart"][0]
        assert len(reads) == 2

    def test_the_strip_marks_backfilled_months(self, page: Page) -> None:
        sign_in(page)
        page.click("#tab-btn-ledger")
        _wait_for_drawn_strip(page)

        note = page.locator("#ledger-density-backfill")
        expect(note).to_be_visible()
        expect(note).to_contain_text("dated from the MusicBrainz full export")
        expect(note).to_contain_text("2025-11, 2025-12, 2026-02.")
        expect(page.locator("#ledger-density-legend .ledger-swatch-backfill")).to_have_count(2)
        labels = page.evaluate("() => window.adminDashboard.state.ledgerChart.data.labels")
        assert labels[0] == "2025-11"
        assert labels[-1] == "2026-09"
        assert len(labels) == 11

    def test_the_works_table_pages_and_filters(self, page: Page) -> None:
        works = [
            {
                "gm_id": f"00000000-0000-4000-8000-{index:012d}",
                "providers": [{"provider": "discogs", "external_id": str(900000 + index)}],
                "editions": 1,
                "evented_editions": 1,
                "repaired_editions": 0,
                "work_disagreements": 0,
                "first_event": "2026-08-01T02:00:00Z",
                "last_event": "2026-08-01T02:00:00Z",
            }
            for index in range(1, 61)
        ]
        fake_catalog("ledger", {"works": works})
        sign_in(page)
        page.click("#tab-btn-ledger")
        expect(page.locator("#ledger-works-body tr[data-work]")).to_have_count(50)
        expect(page.locator("#ledger-page-info")).to_have_text("Page 1 of 2 (60 works)")
        expect(page.locator("#ledger-prev-btn")).to_be_disabled()

        page.click("#ledger-next-btn")
        expect(page.locator("#ledger-works-body tr[data-work]")).to_have_count(10)
        expect(page.locator("#ledger-page-info")).to_have_text("Page 2 of 2 (60 works)")
        expect(page.locator("#ledger-next-btn")).to_be_disabled()

        with page.expect_request(lambda request: "/admin/api/identity/ledger/works?" in request.url and "event=" in request.url) as event_read:
            page.select_option("#ledger-event-filter", "link_changed")
        assert parse_qs(urlparse(event_read.value.url).query) == {"page": ["1"], "page_size": ["50"], "event": ["link_changed"]}

        page.fill("#ledger-since", "2025-11-01")
        with page.expect_request(lambda request: "until=" in request.url) as dated_read:
            page.fill("#ledger-until", "2026-02-28")
        query = parse_qs(urlparse(dated_read.value.url).query)
        assert query["since"] == ["2025-11-01T00:00:00Z"]
        assert query["until"] == ["2026-03-01T00:00:00Z"]
        expect(page.locator("#ledger-works-error")).to_be_hidden()

    def test_a_work_drills_down_at_a_chosen_as_of(self, page: Page) -> None:
        sign_in(page)
        page.click("#tab-btn-ledger")
        page.click(f'#ledger-works-body tr[data-work="{LEDGER_WORK}"] button.ledger-work-link')

        drill = page.locator("#ledger-work")
        expect(drill).to_be_visible()
        expect(page.locator("#ledger-work-id")).to_have_text(LEDGER_WORK)
        expect(page.locator("#ledger-work-status")).to_have_text("Read at now · 2 of 2 editions")
        edition = page.locator(f'[data-edition="{LEDGER_EDITION}"]')
        expect(edition.locator(".ledger-events li")).to_have_count(3)
        expect(edition.locator(".ledger-badge").first).to_have_text("linked")
        expect(edition).to_contain_text("superseded 2026-09-03 12:00 UTC (catalog_reattachment)")
        split = page.locator(f'[data-edition="{LEDGER_SPLIT_EDITION}"]')
        expect(split.locator(".ledger-disagreement")).to_have_text("Work disagreement")
        expect(split.locator("dd button.ledger-work-link")).to_have_text(LEDGER_OTHER_WORK)

        backfilled = page.locator('#ledger-work .ledger-events li[data-dated-from="musicbrainz_fullexport"]')
        expect(backfilled).to_have_count(1)
        expect(backfilled.locator(".ledger-backfill")).to_have_text("dated from the MusicBrainz full export")
        expect(page.locator('#ledger-work .ledger-events li[data-dated-from="groovemap"] .ledger-backfill')).to_have_count(0)

        page.fill("#ledger-as-of", "2026-08-15T00:00")
        with page.expect_response(lambda response: "as_of=" in response.url) as read:
            page.click("#ledger-as-of-btn")
        assert parse_qs(urlparse(read.value.url).query)["as_of"] == ["2026-08-15T00:00:00Z"]
        expect(page.locator("#ledger-work-status")).to_have_text("Read at 2026-08-15 00:00 UTC · 2 of 2 editions")
        expect(edition.locator(".ledger-events li")).to_have_count(2)
        expect(edition).not_to_contain_text("catalog_reattachment")

        page.click("#ledger-now-btn")
        expect(edition.locator(".ledger-events li")).to_have_count(3)
        page.click("#ledger-work-close")
        expect(drill).to_be_hidden()

    def test_the_tab_fits_a_phone(self, page: Page) -> None:
        page.set_viewport_size(PHONE)
        sign_in(page)
        page.click("#tab-btn-ledger")
        size = _wait_for_drawn_strip(page)
        assert size["chart"] is not None
        assert size["chart"][0] <= PHONE["width"]
        page.click(f'#ledger-works-body tr[data-work="{LEDGER_WORK}"] button.ledger-work-link')
        expect(page.locator(f'[data-edition="{LEDGER_EDITION}"] .ledger-events li')).to_have_count(3)

        faults = page.evaluate(LAYOUT_FAULTS)
        assert faults["overflow"] == 0, faults
        assert faults["spills"] == [], faults
        # The works table scrolls inside its card rather than widening the page.
        assert faults["tableRight"] <= 1, faults
        detail = page.locator(f'[data-edition="{LEDGER_EDITION}"] .ledger-events li .ledger-detail').first
        item = page.locator(f'[data-edition="{LEDGER_EDITION}"] .ledger-events li').first
        detail_box, item_box = detail.bounding_box(), item.bounding_box()
        assert detail_box is not None
        assert item_box is not None
        # Stacked below the time and type at this width, the detail takes the row's full width.
        assert detail_box["width"] == pytest.approx(item_box["width"], abs=2)


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestLedgerFreshness:
    def test_every_view_says_when_the_ledger_was_refreshed(self, page: Page) -> None:
        sign_in(page)
        page.click("#tab-btn-ledger")
        expect(page.locator("#ledger-works-body tr[data-work]")).to_have_count(2)
        expect(page.locator("#ledger-density-asof")).to_have_text("Ledger as of 2026-10-25 21:05 UTC")
        expect(page.locator("#ledger-works-asof")).to_have_text("Ledger as of 2026-10-25 21:05 UTC")
        expect(page.locator("#ledger-building")).to_be_hidden()

        page.click(f'#ledger-works-body tr[data-work="{LEDGER_WORK}"] button.ledger-work-link')
        expect(page.locator("#ledger-work-asof")).to_have_text("Ledger as of 2026-10-25 21:05 UTC")

    def test_a_ledger_that_is_being_built_is_a_state_and_is_read_again_when_built(self, page: Page) -> None:
        fake_catalog("ledger-building", {"enabled": True, "retry_after": 1})
        reads = _record_ledger_reads(page)
        sign_in(page)
        page.click("#tab-btn-ledger")

        notice = page.locator("#ledger-building")
        expect(notice).to_be_visible()
        expect(notice).to_contain_text("The ledger is being built; it refreshes after loads and identity runs")
        expect(page.locator("#ledger-density-error")).to_be_hidden()
        expect(page.locator("#ledger-works-error")).to_be_hidden()
        expect(page.locator("#ledger-density-asof")).to_have_text("Ledger not yet refreshed")
        expect(page.locator("#ledger-works-body")).to_contain_text("until the ledger is built")
        expect(page.locator("#ledger-works-body tr[data-work]")).to_have_count(0)

        # It asks again after Retry-After, and keeps waiting while the answer is still 503.
        for _ in range(100):
            if sum(urlparse(url).path.endswith("/ledger/works") for url in reads) >= 2:
                break
            page.wait_for_timeout(100)
        assert sum(urlparse(url).path.endswith("/ledger/works") for url in reads) >= 2
        expect(notice).to_be_visible()

        # One work is read live, so it opens, and says the ledger was never refreshed.
        page.evaluate("(id) => window.adminDashboard.openLedgerWork(id)", LEDGER_WORK)
        expect(page.locator("#ledger-work-asof")).to_have_text("Ledger not yet refreshed")
        expect(page.locator(f'[data-edition="{LEDGER_EDITION}"]')).to_be_visible()

        fake_catalog("ledger-building", {"enabled": False})
        expect(page.locator("#ledger-works-body tr[data-work]")).to_have_count(2, timeout=10000)
        expect(notice).to_be_hidden()
        expect(page.locator("#ledger-density-asof")).to_have_text("Ledger as of 2026-10-25 21:05 UTC")
        expect(page.locator("#ledger-works-asof")).to_have_text("Ledger as of 2026-10-25 21:05 UTC")
        _wait_for_drawn_strip(page)
        settled = len(reads)
        page.wait_for_timeout(1500)
        assert len(reads) == settled, "the tab kept reading after the ledger was built"
