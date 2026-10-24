"""Playwright tests for the dashboard UI."""
# mypy: disable-error-code="no-untyped-def"

import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_console import CONSOLE_URL, DATA_ROUTES, freeze_live_feed, sign_in


DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

# Push 36 readings 2 s apart (70 s of server time) for every queue the drawn topology names:
# `tableinator · releases` (or the second consumer's) stops acknowledging while connected, the
# first consumer's MusicBrainz releases DLQ grows, and `graphinator · artists` holds a flat DLQ.
# Everything else flows. The history is reset first, so live readings do not mix in.
SCENARIO = """
() => {
  const dashboard = window.dashboard;
  dashboard._resetHistory();
  const start = Date.now() + 1000;
  for (let i = 0; i < 36; i++) {
    const pipelines = {};
    for (const p of dashboard.topology.pipelines) {
      const queues = [];
      p.consumers.forEach((c, ci) => c.queues.forEach((q, qi) => {
        let messages = 200 + qi * 900 + ci * 15000, ack = 400 + qi * 50;
        if (p.id === 'discogs' && ci === 1 && q.entity === 'releases') { messages = 442000 + i * 1200; ack = 0; }
        queues.push({ name: q.name, messages, messages_ready: Math.max(0, messages - 200), messages_unacknowledged: Math.min(200, messages),
                      consumers: 1, message_rate: ack + 5, ack_rate: ack });
        let dlq = 0;
        if (p.id === 'discogs' && ci === 0 && q.entity === 'artists') dlq = 12;
        if (p.id === 'musicbrainz' && ci === 0 && q.entity === 'releases') dlq = 3000 + i * 40;
        queues.push({ name: q.dead_letter_queue, messages: dlq, messages_ready: dlq, messages_unacknowledged: 0, consumers: 0, message_rate: 0, ack_rate: 0 });
      }));
      pipelines[p.id] = { services: [], queues, queues_available: true };
    }
    dashboard.updateDashboard({ timestamp: new Date(start + i * 2000).toISOString(), pipelines, databases: [] });
  }
}
"""

# Words in the matrix rendered across more than one line, and matrix parts whose text spills out.
LAYOUT_FAULTS = """
() => {
  const root = document.getElementById('pipelines');
  const broken = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    for (const match of node.textContent.matchAll(/\\S+/g)) {
      const range = document.createRange();
      range.setStart(node, match.index);
      range.setEnd(node, match.index + match[0].length);
      const lines = new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top)));
      if (lines.size > 1) broken.push(match[0]);
    }
  }
  const spills = [...root.querySelectorAll('.mx-cell, .mx-colhead, .mx-rowhead')]
    .filter((el) => el.offsetParent !== null && el.scrollWidth > el.clientWidth + 1).map((el) => el.id || el.textContent.trim());
  const page = document.documentElement;
  return { overflow: Math.max(0, page.scrollWidth - page.clientWidth), broken, spills };
}
"""


def two_x_consumers(route: Route) -> None:
    """Serve the real topology with two hypothetical consumers added to every pipeline."""
    topology = route.fetch().json()
    for pipeline in topology["pipelines"]:
        prefix = pipeline["queue_prefix"]
        for extra, store in (("searchinator", "postgresql"), ("embedinator", "neo4j")):
            name = extra if pipeline["id"] == "discogs" else f"brainz{extra}"
            queues = [
                {
                    "entity": e,
                    "name": f"{prefix}-{name}-{e}",
                    "dead_letter_exchange": f"{prefix}-{name}-{e}.dlx",
                    "dead_letter_queue": f"{prefix}-{name}-{e}.dlq",
                }
                for e in pipeline["entity_types"]
            ]
            pipeline["consumers"].append({"name": name, "label": name.capitalize(), "datastore": store, "queues": queues})
    route.fulfill(json=topology)


# Both pipelines' matrices fit in this much height at 1440 px, so with the ~190 px console header
# and tab bar above them both pipelines, and every fault, are on a 900 px first screen.
MATRICES_BUDGET = 700


def matrices_height(page: Page) -> float:
    """Height of the legend and every pipeline matrix together, independent of the page chrome above."""
    box = page.locator("#pipelines").bounding_box()
    assert box is not None
    return box["height"]


def show_scenario(page: Page, viewport: dict[str, int]) -> None:
    """Sign in at ``viewport``, freeze the live feed, and draw the fault scenario."""
    page.set_viewport_size(viewport)
    sign_in(page)
    freeze_live_feed(page)
    page.evaluate(SCENARIO)


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestDashboardUI:
    """Test the console's live Overview, signed in as an admin.

    The test server is automatically started by the test_server fixture, together with the fake
    catalog-api the console validates sessions against.

    To run these tests:
    uv run pytest tests/dashboard/test_dashboard_ui.py -m e2e --browser chromium
    """

    def test_dashboard_loads(self, page: Page) -> None:
        """Test that the dashboard page loads successfully."""
        sign_in(page)

        # Page title is set in <title> tag
        expect(page).to_have_title("GrooveMap Operations Console")

        # Deep Space rebrand: heading contains brand name and tagline
        heading = page.locator("h1")
        expect(heading).to_contain_text("GrooveMap")

    def test_service_cards_display(self, page: Page) -> None:
        """Every Discogs service shows in its matrix: the producer in the pipeline head, consumers as column heads."""
        sign_in(page)

        service_ids = ["extractor-discogs", "graphinator", "tableinator"]
        for sid in service_ids:
            card = page.locator(f"#service-{sid}")
            expect(card).to_be_visible(timeout=10000)

    def test_the_overview_is_an_entity_by_consumer_matrix_without_dials_or_bar_chart(self, page: Page) -> None:
        """Rows are entity types and columns consumers with their datastore; the dials and bar chart are gone."""
        page.set_viewport_size(DESKTOP)
        sign_in(page)
        matrix = page.locator('[data-matrix="discogs"]')
        expect(matrix).to_be_visible(timeout=10000)
        expect(matrix.locator(".mx-colhead")).to_have_text([re.compile(r"Graphinator\s*Neo4j"), re.compile(r"Tableinator\s*PostgreSQL")])
        expect(matrix.locator(".mx-entity")).to_have_text(["Masters", "Releases", "Artists", "Labels"])
        expect(matrix.locator("button.mx-cell")).to_have_count(8)
        expect(page.locator("[data-matrix-legend]")).to_contain_text("Cell colour = lag")
        # The live test feed reports graphinator · artists: 10 messages.
        expect(page.locator("#discogs-graphinator-artists-cell .mx-n")).to_have_text("10", timeout=10000)
        expect(page.locator('[id*="rate-circle"], [id*="rate-text"], [id*="-bar-"]')).to_have_count(0)
        expect(page.get_by_text("Queue Size Metrics")).to_have_count(0)
        expect(page.locator("#dlq-toggle")).to_have_count(0)

    def test_desktop_marks_a_stalled_queue_and_a_growing_dlq_on_the_first_screen(self, page: Page) -> None:
        """At 1440 x 900 both faults are critical (glyph, label, hatch), nothing else is, and both pipelines fit one screen."""
        show_scenario(page, DESKTOP)
        stalled = page.locator("#discogs-tableinator-releases-cell")
        expect(stalled).to_have_attribute("data-state", "crit")
        expect(stalled).to_have_class(re.compile(r"\bmx-crit\b"))
        expect(stalled.locator(".mx-flag")).to_have_text("◆ STALLED · no acks 1m")
        growing = page.locator("#musicbrainz-brainzgraphinator-releases-cell")
        expect(growing).to_have_attribute("data-state", "crit")
        expect(growing.locator(".mx-dlq")).to_have_text("◆ DLQ +1.4k / 5m")
        flat = page.locator("#discogs-graphinator-artists-cell")
        expect(flat).to_have_attribute("data-state", "warn")
        expect(flat.locator(".mx-dlq")).to_have_text("▲ DLQ 12")
        expect(page.locator("#pipelines .mx-cell[data-state=crit]")).to_have_count(2)
        assert matrices_height(page) <= MATRICES_BUDGET
        assert page.evaluate(LAYOUT_FAULTS) == {"overflow": 0, "broken": [], "spills": []}

    def test_phone_shows_the_matrix_without_overflow(self, page: Page) -> None:
        """At 390 px the two-consumer grid stays a grid, inside the page, with every word whole."""
        show_scenario(page, PHONE)
        expect(page.locator('[data-matrix="discogs"]')).not_to_have_class(re.compile(r"mx-stacked"))
        expect(page.locator("#discogs-tableinator-releases-cell")).to_have_attribute("data-state", "crit")
        assert page.evaluate(LAYOUT_FAULTS) == {"overflow": 0, "broken": [], "spills": []}

    def test_a_healthy_console_is_operational_and_the_attention_bar_is_quiet(self, page: Page) -> None:
        """No fault: the bar is hidden, and the header reads Operational rather than Unknown."""
        page.set_viewport_size(DESKTOP)
        sign_in(page)
        expect(page.locator("#discogs-graphinator-artists-cell .mx-n")).to_have_text("10", timeout=10000)
        expect(page.locator("#operational-status-text")).to_have_text("Operational")
        expect(page.locator("#attention-bar")).to_be_hidden()

    def test_desktop_surfaces_a_musicbrainz_fault_at_the_top_of_the_first_screen(self, page: Page) -> None:
        """Every fault is listed above the tabs, before the matrices, and the header reads Critical."""
        show_scenario(page, DESKTOP)
        bar = page.locator("#attention-bar")
        expect(bar).to_be_visible()
        expect(page.locator("#attention-summary")).to_have_text("3 items need attention · 2 critical · 1 warning")
        fault = bar.get_by_role("button", name=re.compile(r"MusicBrainz Releases → Brainzgraphinator: DLQ \+1\.4k"))
        expect(fault).to_be_visible()
        expect(bar.get_by_role("button", name=re.compile(r"Discogs Releases → Tableinator: STALLED"))).to_be_visible()
        expect(bar.get_by_role("button", name=re.compile(r"Warning: Discogs Artists → Graphinator: DLQ 12"))).to_be_visible()
        assert page.locator("#attention-bar").bounding_box()["y"] < page.locator("nav").first.bounding_box()["y"]
        assert fault.bounding_box()["y"] + fault.bounding_box()["height"] <= DESKTOP["height"]
        expect(page.locator("#operational-status-text")).to_have_text("Critical")

    def test_phone_surfaces_a_musicbrainz_fault_without_a_scroll(self, page: Page) -> None:
        """At 390 x 844 the matrices start below the fold, but every fault is on the first screen."""
        show_scenario(page, PHONE)
        bar = page.locator("#attention-bar")
        expect(bar).to_be_visible()
        assert page.evaluate("() => window.scrollY") == 0
        matrix_top = page.locator('[data-matrix="discogs"]').bounding_box()["y"]
        for name in (
            r"Discogs Releases → Tableinator: STALLED",
            r"MusicBrainz Releases → Brainzgraphinator: DLQ",
            r"Discogs Artists → Graphinator: DLQ 12",
        ):
            item = bar.get_by_role("button", name=re.compile(name))
            expect(item).to_be_in_viewport(ratio=1)
            assert item.bounding_box()["y"] < matrix_top
        assert page.evaluate(LAYOUT_FAULTS)["overflow"] == 0

    def test_an_attention_item_opens_its_cell_drawer_from_any_tab(self, page: Page) -> None:
        """The bar is on every tab; its queue item returns to the Overview and opens the cell's drawer."""
        show_scenario(page, DESKTOP)
        page.click("#tab-btn-storage")
        expect(page.locator("#tab-storage")).to_be_visible()
        expect(page.locator("#attention-bar")).to_be_visible()
        page.get_by_role("button", name=re.compile(r"MusicBrainz Releases → Brainzgraphinator")).click()
        expect(page.locator("#tab-overview")).to_be_visible()
        expect(page.locator("#queue-drawer")).to_be_visible()
        expect(page.locator("#queue-drawer-title")).to_have_text("Releases → Brainzgraphinator")

    def test_two_x_consumers_stack_on_a_phone_and_stay_a_grid_on_desktop(self, page: Page) -> None:
        """Four consumers per pipeline: self-labelled stacked cells below 480 px, no overflow or broken words."""
        page.route("**/api/topology", two_x_consumers)
        show_scenario(page, PHONE)
        for pipeline in ("discogs", "musicbrainz"):
            matrix = page.locator(f'[data-matrix="{pipeline}"]')
            expect(matrix).to_have_class(re.compile(r"\bmx-stacked\b"))
            expect(matrix.locator("button.mx-cell")).to_have_count(16)
        expect(page.locator("#discogs-embedinator-releases-cell .mx-cname")).to_be_visible()
        expect(page.locator("#discogs-embedinator-releases-cell .mx-cname")).to_have_text("Embedinator")
        expect(page.locator("#discogs-tableinator-releases-cell")).to_have_attribute("data-state", "crit")
        assert page.evaluate(LAYOUT_FAULTS) == {"overflow": 0, "broken": [], "spills": []}

        page.set_viewport_size(DESKTOP)
        expect(page.locator('[data-matrix="discogs"]')).not_to_have_class(re.compile(r"mx-stacked"))
        expect(page.locator("#discogs-embedinator-releases-cell .mx-cname")).to_be_hidden()
        # Twice the consumers only adds columns: both pipelines still fit the first screen.
        assert matrices_height(page) <= MATRICES_BUDGET
        assert page.evaluate(LAYOUT_FAULTS) == {"overflow": 0, "broken": [], "spills": []}

    def test_a_cell_opens_the_detail_drawer_from_the_keyboard(self, page: Page) -> None:
        """A focused cell opens the drawer with its queue's numbers; Escape closes it and focus returns."""
        show_scenario(page, DESKTOP)
        cell = page.locator("#discogs-tableinator-releases-cell")
        cell.focus()
        page.keyboard.press("Enter")
        drawer = page.locator("#queue-drawer")
        expect(drawer).to_be_visible()
        expect(page.locator("#queue-drawer-title")).to_have_text("Releases → Tableinator")
        expect(page.locator("#queue-drawer-state")).to_have_text("◆ STALLED · no acks 1m")
        numbers = page.locator("#queue-drawer-numbers")
        expect(numbers).to_contain_text("groovemap-discogs-tableinator-releases")
        expect(numbers).to_contain_text("484,000")
        expect(numbers).to_contain_text("+42,000 over 1m")

        page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
        expect(cell).to_be_focused()

    def test_unavailable_queue_metrics_are_explicit_and_recover(self, page: Page) -> None:
        """A failed broker scrape shows "Queue metrics unavailable" and unavailable cells, then clears."""
        sign_in(page)
        expect(page.locator("#discogs-graphinator-masters-cell")).to_be_visible(timeout=10000)
        # Stop the live feed overwriting the payloads this test injects.
        freeze_live_feed(page)
        service = {"name": "graphinator", "status": "healthy", "last_seen": None, "current_task": None, "progress": None, "error": None}
        queue = {
            "name": "groovemap-discogs-graphinator-masters",
            "messages": 3,
            "messages_ready": 3,
            "messages_unacknowledged": 0,
            "consumers": 1,
            "message_rate": 2.0,
            "ack_rate": 1.0,
        }
        banner = page.locator('#pipeline-discogs [data-queues-unavailable="discogs"]')
        cell = page.locator("#discogs-graphinator-masters-cell")

        def push(queues: list, available: bool) -> None:
            payload = {"pipelines": {"discogs": {"services": [service], "queues": queues, "queues_available": available}}, "databases": []}
            page.evaluate("(data) => window.dashboard.updateDashboard(data)", payload)

        push([], False)
        # The test server does not serve tailwind.css, so assert the `hidden` class, not computed visibility.
        hidden = re.compile(r"(^|\s)hidden(\s|$)")
        expect(banner.first).not_to_have_class(hidden)
        expect(banner.first).to_have_text("Queue metrics unavailable")
        expect(cell).to_have_class(re.compile(r"\bmx-unavailable\b"))
        expect(cell.locator(".mx-meta")).to_have_text("unavailable")
        expect(page.locator('[data-matrix="discogs"] .mx-cell.mx-unavailable')).to_have_count(8)

        push([queue], True)
        expect(banner.first).to_have_class(hidden)
        expect(cell).not_to_have_class(re.compile(r"\bmx-unavailable\b"))
        expect(cell.locator(".mx-n")).to_have_text("3")

    def test_a_synthetic_consumer_and_entity_type_render_without_markup_changes(self, page: Page) -> None:
        """The Overview draws whatever /api/topology describes: a third consumer and a new entity type appear."""
        entities = ["artists", "release-groups", "tracks"]

        def queues_of(consumer: str) -> list[dict]:
            return [
                {
                    "entity": entity,
                    "name": f"acme-synthetic-{consumer}-{entity}",
                    "dead_letter_exchange": f"acme-synthetic-{consumer}-{entity}.dlx",
                    "dead_letter_queue": f"acme-synthetic-{consumer}-{entity}.dlq",
                }
                for entity in entities
            ]

        topology = {
            "pipelines": [
                {
                    "id": "synthetic",
                    "label": "Synthetic",
                    "queue_prefix": "acme-synthetic",
                    "entity_types": entities,
                    "producer": {"name": "extractor-synthetic", "label": "Extractor", "exchanges": []},
                    "consumers": [
                        {"name": name, "label": name.capitalize(), "datastore": store, "queues": queues_of(name)}
                        for name, store in (("alpha", "neo4j"), ("beta", "postgresql"), ("gamma", "valkey"))
                    ],
                    "dead_letter_queues": [],
                }
            ],
            "datastores": [{"id": "neo4j", "label": "Neo4j"}, {"id": "valkey", "label": "Valkey"}],
        }
        page.route("**/api/topology", lambda route: route.fulfill(json=topology))
        sign_in(page)
        freeze_live_feed(page)
        queue = {
            "name": "acme-synthetic-gamma-tracks",
            "messages": 7,
            "messages_ready": 7,
            "messages_unacknowledged": 0,
            "consumers": 1,
            "message_rate": 1.5,
            "ack_rate": None,
        }
        payload = {"pipelines": {"synthetic": {"services": [], "queues": [queue], "queues_available": True}}, "databases": []}
        page.evaluate("(data) => window.dashboard.updateDashboard(data)", payload)

        expect(page.locator("#service-gamma")).to_be_visible(timeout=10000)
        expect(page.locator("#synthetic-gamma-tracks-cell .mx-n")).to_have_text("7")
        # Its ack rate is unknown: the lag is unknown, never a stall.
        expect(page.locator("#synthetic-gamma-tracks-cell .mx-meta")).to_have_text("lag unknown")
        expect(page.locator("#synthetic-gamma-tracks-cell")).to_have_attribute("data-state", "ok")
        expect(page.locator("#synthetic-alpha-release-groups-cell")).to_be_visible()
        expect(page.locator("#service-gamma [data-datastore]")).to_have_text("Valkey")

    def test_database_cards_display(self, page: Page) -> None:
        """Test that database cards are displayed."""
        sign_in(page)

        # Each database has a dedicated card element
        neo4j_card = page.locator("#db-neo4j")
        expect(neo4j_card).to_be_visible(timeout=10000)

        postgresql_card = page.locator("#db-postgresql")
        expect(postgresql_card).to_be_visible(timeout=10000)

        # Check database name labels
        for name in ["Neo4j", "PostgreSQL"]:
            label = page.locator(".database-name", has_text=name)
            expect(label).to_be_visible(timeout=10000)

    def test_websocket_connection(self, page: Page) -> None:
        """Test that WebSocket connection is established."""
        sign_in(page)

        # The connection-status widget lives in the event-log header
        connection_status = page.locator(".connection-status")
        expect(connection_status).to_be_visible(timeout=10000)

        # Status text changes to "Connected" once the WS handshake completes
        status_text = page.locator(".status-text")
        expect(status_text).to_have_text("Connected", timeout=15000)

        # Status indicator acquires the .connected class
        status_indicator = page.locator(".status-indicator")
        expect(status_indicator).to_have_class(re.compile(r"connected"), timeout=10000)

    def test_activity_log_display(self, page: Page) -> None:
        """Test that the activity log is displayed."""
        sign_in(page)

        # Event log panel is present
        activity_log = page.locator("#activityLog")
        expect(activity_log).to_be_visible(timeout=10000)

        # Should have at least one log entry (connection message)
        log_entries = page.locator(".log-entry")
        expect(log_entries.first).to_be_visible(timeout=15000)

    def test_responsive_design(self, page: Page) -> None:
        """Test that the dashboard is responsive."""
        sign_in(page)

        # Desktop viewport
        page.set_viewport_size({"width": 1440, "height": 900})
        expect(page.locator("header")).to_be_visible(timeout=10000)

        # Mobile viewport — page should still render without errors
        page.set_viewport_size({"width": 375, "height": 667})
        expect(page.locator("#activityLog")).to_be_visible(timeout=10000)

        # Service cards remain in the DOM regardless of viewport
        expect(page.locator("#service-extractor-discogs")).to_be_visible(timeout=10000)

    def test_api_endpoints(self, page: Page) -> None:
        """The data routes answer the signed-in session's bearer, and nothing without it."""
        sign_in(page)
        token = page.evaluate("() => localStorage.getItem('admin_token')")
        headers = {"Authorization": f"Bearer {token}"}

        for path in DATA_ROUTES:
            assert page.request.get(f"{CONSOLE_URL}{path}").status == 401, path

        # Test metrics endpoint (pipeline-grouped structure)
        response = page.request.get(f"{CONSOLE_URL}/api/metrics", headers=headers)
        assert response.ok
        data = response.json()
        assert "pipelines" in data
        assert "databases" in data
        assert "timestamp" in data

        # Test services endpoint (grouped by pipeline)
        response = page.request.get(f"{CONSOLE_URL}/api/services", headers=headers)
        assert response.ok
        services = response.json()
        assert isinstance(services, dict)

        # Test queues endpoint (grouped by pipeline)
        response = page.request.get(f"{CONSOLE_URL}/api/queues", headers=headers)
        assert response.ok
        queues = response.json()
        assert isinstance(queues, dict)

        # Test databases endpoint (flat list, unchanged)
        response = page.request.get(f"{CONSOLE_URL}/api/databases", headers=headers)
        assert response.ok
        databases = response.json()
        assert isinstance(databases, list)
