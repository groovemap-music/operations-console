"""Playwright tests for the dashboard UI."""
# mypy: disable-error-code="no-untyped-def"

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_console import CONSOLE_URL, DATA_ROUTES, freeze_live_feed, sign_in


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
        """Test that service cards are displayed."""
        sign_in(page)

        # Each Discogs service has a dedicated card element with id="service-<name>"
        service_ids = ["extractor-discogs", "graphinator", "tableinator"]
        for sid in service_ids:
            card = page.locator(f"#service-{sid}")
            expect(card).to_be_visible(timeout=10000)

    def test_queue_section_display(self, page: Page) -> None:
        """Test that queue metrics sections are displayed."""
        sign_in(page)

        # Bar chart section heading (scoped to Discogs pipeline to avoid strict mode violation)
        queue_heading = page.locator("#pipeline-discogs h2", has_text="Queue Size Metrics")
        expect(queue_heading).to_be_visible(timeout=10000)

        # Processing rates section headings (split into publish and ack panels)
        publish_rates_heading = page.locator("#pipeline-discogs h2", has_text="Publish Rates")
        expect(publish_rates_heading).to_be_visible(timeout=10000)

        ack_rates_heading = page.locator("#pipeline-discogs h2", has_text="Ack Rates")
        expect(ack_rates_heading).to_be_visible(timeout=10000)

        # SVG rate circles are rendered (prefixed with pipeline name)
        rate_circle = page.locator("#discogs-rate-circle-graphinator-masters-publish")
        expect(rate_circle).to_be_visible(timeout=10000)

    def test_unavailable_queue_metrics_are_explicit_and_recover(self, page: Page) -> None:
        """A failed broker scrape shows "Queue metrics unavailable" and N/A dials, then clears."""
        sign_in(page)
        expect(page.locator("#discogs-rate-circle-graphinator-masters-publish")).to_be_visible(timeout=10000)
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
        dial = page.locator("#discogs-rate-text-graphinator-masters-publish")

        def push(queues: list, available: bool) -> None:
            payload = {"pipelines": {"discogs": {"services": [service], "queues": queues, "queues_available": available}}, "databases": []}
            page.evaluate("(data) => window.dashboard.updateDashboard(data)", payload)

        push([], False)
        # The test server does not serve tailwind.css, so assert the `hidden` class, not computed visibility.
        hidden = re.compile(r"(^|\s)hidden(\s|$)")
        expect(banner.first).not_to_have_class(hidden)
        expect(banner.first).to_have_text("Queue metrics unavailable")
        expect(dial).to_have_text("N/A")

        push([queue], True)
        expect(banner.first).to_have_class(hidden)
        expect(dial).to_have_text("2.0")

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
        expect(page.locator("#synthetic-gamma-tracks-count")).to_have_text("7")
        expect(page.locator("#synthetic-rate-text-gamma-tracks-publish")).to_have_text("1.5")
        expect(page.locator("#synthetic-rate-text-gamma-tracks-ack")).to_have_text("—")
        expect(page.locator("#synthetic-bar-tracks-gamma")).to_be_visible()
        expect(page.locator("#service-gamma [data-datastore]")).to_have_text("→ Valkey")

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
