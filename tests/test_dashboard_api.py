"""API tests for the dashboard using FastAPI TestClient."""

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


if TYPE_CHECKING:
    from tests.fake_catalog_api import FakeCatalogApi


def _auth(catalog: FakeCatalogApi) -> dict[str, str]:
    return {"Authorization": f"Bearer {catalog.mint()}"}


class TestDashboardAPI:
    """Test the dashboard API endpoints."""

    @pytest.fixture
    def test_client(
        self,
        mock_dashboard_config: Any,
        dashboard_mock_neo4j_driver: Any,
        dashboard_mock_httpx_client: Any,
        dashboard_mock_psycopg_connect: Any,
        fake_catalog: FakeCatalogApi,
    ) -> TestClient:
        """Create a test client, signed in as an admin, with mocked dependencies."""
        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=dashboard_mock_neo4j_driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("httpx.AsyncClient") as mock_httpx_class,
        ):
            # Configure httpx.AsyncClient to return our mock instance
            mock_httpx_class.return_value = dashboard_mock_httpx_client

            # Import the app after patching
            from dashboard.dashboard import app

            return TestClient(app, headers=_auth(fake_catalog))

    def test_root_endpoint(self, test_client: TestClient) -> None:
        """Test that the root endpoint returns the index page."""
        response = test_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_metrics_endpoint(self, test_client: TestClient) -> None:
        """Test the metrics API endpoint."""
        # First, trigger the lifespan events by making a request
        test_client.get("/")

        response = test_client.get("/api/metrics")
        assert response.status_code == 200
        data = response.json()

        # Verify response is valid JSON (structural assertions are covered
        # by TestDashboardAPIIntegration.test_metrics_endpoint)
        assert isinstance(data, dict)

    def test_static_files(self, test_client: TestClient) -> None:
        """Test that static files are served."""
        # Static files are mounted at root, not under /static
        # Test JavaScript (CSS is tailwind.css, a build artifact not present in source)
        response = test_client.get("/dashboard.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    @pytest.mark.parametrize("path", ["/admin", "/admin.html"])
    def test_admin_page_redirects_to_the_console_shell(self, test_client: TestClient, path: str) -> None:
        """The admin tabs moved into the shell at /; old admin links redirect there."""
        response = test_client.get(path, follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/"

    def test_services_endpoint_without_dashboard(
        self,
        mock_dashboard_config: Any,
        dashboard_mock_neo4j_driver: Any,
        dashboard_mock_psycopg_connect: Any,
        fake_catalog: FakeCatalogApi,
    ) -> None:
        """Test /api/services endpoint when dashboard is not initialized."""
        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=dashboard_mock_neo4j_driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("dashboard.dashboard.dashboard", None),  # Force dashboard to None
        ):
            from dashboard.dashboard import app

            test_client = TestClient(app, raise_server_exceptions=False, headers=_auth(fake_catalog))

            response = test_client.get("/api/services")
            assert response.status_code == 200
            assert response.json() == {}

    def test_queues_endpoint_without_dashboard(
        self,
        mock_dashboard_config: Any,
        dashboard_mock_neo4j_driver: Any,
        dashboard_mock_psycopg_connect: Any,
        fake_catalog: FakeCatalogApi,
    ) -> None:
        """Test /api/queues endpoint when dashboard is not initialized."""
        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=dashboard_mock_neo4j_driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("dashboard.dashboard.dashboard", None),  # Force dashboard to None
        ):
            from dashboard.dashboard import app

            test_client = TestClient(app, raise_server_exceptions=False, headers=_auth(fake_catalog))

            response = test_client.get("/api/queues")
            assert response.status_code == 200
            assert response.json() == {}

    def test_databases_endpoint_without_dashboard(
        self,
        mock_dashboard_config: Any,
        dashboard_mock_neo4j_driver: Any,
        dashboard_mock_psycopg_connect: Any,
        fake_catalog: FakeCatalogApi,
    ) -> None:
        """Test /api/databases endpoint when dashboard is not initialized."""
        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=dashboard_mock_neo4j_driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("dashboard.dashboard.dashboard", None),  # Force dashboard to None
        ):
            from dashboard.dashboard import app

            test_client = TestClient(app, raise_server_exceptions=False, headers=_auth(fake_catalog))

            response = test_client.get("/api/databases")
            assert response.status_code == 200
            assert response.json() == []


class TestQueuesEndpointServesLiveSnapshot:
    """/api/queues reads the loop's latest snapshot and never scrapes the broker itself."""

    @pytest.fixture(autouse=True)
    def _signed_in(self, fake_catalog: FakeCatalogApi) -> None:
        self.headers = _auth(fake_catalog)

    def _get(self, snapshot: Any) -> Any:
        from unittest.mock import AsyncMock, Mock

        fake = Mock(latest_metrics=snapshot, get_queue_info=AsyncMock())
        with patch("dashboard.dashboard.dashboard", fake):
            from dashboard.dashboard import app

            response = TestClient(app, raise_server_exceptions=False, headers=self.headers).get("/api/queues")
        fake.get_queue_info.assert_not_called()
        return response

    def test_serves_snapshot_with_null_for_unavailable_pipeline(self) -> None:
        from datetime import UTC, datetime

        from dashboard.dashboard import PipelineMetrics, QueueInfo, SystemMetrics

        queue = QueueInfo(name="q", messages=1, messages_ready=1, messages_unacknowledged=0, consumers=1, message_rate=2.0, ack_rate=1.0)
        snapshot = SystemMetrics(
            pipelines={
                "discogs": PipelineMetrics(services=[], queues=[queue]),
                "musicbrainz": PipelineMetrics(services=[], queues=[], queues_available=False),
            },
            databases=[],
            timestamp=datetime.now(UTC),
        )

        response = self._get(snapshot)

        assert response.status_code == 200
        assert response.json() == {"discogs": [queue.model_dump(mode="json")], "musicbrainz": None}

    def test_empty_before_the_first_poll(self) -> None:
        response = self._get(None)

        assert response.json() == {}
