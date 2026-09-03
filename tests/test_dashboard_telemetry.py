"""Behavioral tests for the operations-console domain OpenTelemetry instruments.

Mirrors groovemap-runtime's own `tests/test_runtime_metrics.py` pattern: an in-memory
`SdkMeterProvider` is installed directly into `common.telemetry`'s module state so the
console's lazily-built instruments bind to it, and the recorded points are read back by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from common import telemetry
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from dashboard import telemetry as console_telemetry


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.metrics.export import Metric

    from dashboard.config import DashboardConfig


class Collector:
    """An in-memory provider whose recorded metrics can be read back by name."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = SdkMeterProvider(metric_readers=[self.reader])

    def metrics(self) -> dict[str, Metric]:
        """Collect once and return every recorded metric by name."""
        data = self.reader.get_metrics_data()
        if data is None:
            return {}
        return {
            metric.name: metric
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        }

    def points(self, name: str) -> list[Any]:
        """Return the data points recorded for one metric name."""
        metric = self.metrics().get(name)
        return [] if metric is None else list(metric.data.data_points)

    def attributes(self, name: str) -> list[dict[str, Any]]:
        """Return the attribute dicts recorded for one metric name."""
        return [dict(point.attributes) for point in self.points(name)]

    def value(self, name: str) -> Any:
        """Return the single value recorded for one instrument."""
        points = self.points(name)
        assert len(points) == 1, f"expected exactly one {name} point, got {points}"
        return points[0].value


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collector]:
    """Install an in-memory provider and make the console instruments build against it."""
    active = Collector()
    monkeypatch.setattr(telemetry, "_provider", active.provider)
    monkeypatch.setattr(telemetry, "_generation", telemetry.provider_generation() + 1)
    console_telemetry.reset_instruments()
    assert telemetry._active_provider() is active.provider
    yield active
    monkeypatch.setattr(telemetry, "_provider", None)
    console_telemetry.reset_instruments()


def test_websocket_connections_reports_up_down_deltas(collector: Collector) -> None:
    """Connect twice, disconnect once — the up-down counter nets to one."""
    console_telemetry.record_websocket_connection_delta(1)
    console_telemetry.record_websocket_connection_delta(1)
    console_telemetry.record_websocket_connection_delta(-1)

    assert collector.value(console_telemetry.WEBSOCKET_CONNECTIONS) == 1


def test_poll_duration_records_target_and_outcome(collector: Collector) -> None:
    """Each poll records a duration histogram point with target and outcome attributes."""
    console_telemetry.record_poll_duration("extractor-discogs", console_telemetry.POLL_OUTCOME_SUCCESS, 0.05)
    console_telemetry.record_poll_duration(console_telemetry.POLL_TARGET_RABBITMQ, console_telemetry.POLL_OUTCOME_FAILURE, 1.2)
    console_telemetry.record_poll_duration(console_telemetry.POLL_TARGET_NEO4J, console_telemetry.POLL_OUTCOME_SUCCESS, 0.01)
    console_telemetry.record_poll_duration(console_telemetry.POLL_TARGET_POSTGRES, console_telemetry.POLL_OUTCOME_SUCCESS, 0.02)
    console_telemetry.record_poll_duration(console_telemetry.POLL_TARGET_LOOP, console_telemetry.POLL_OUTCOME_SUCCESS, 0.3)

    attrs = collector.attributes(console_telemetry.POLL_DURATION)
    assert {"target": "extractor-discogs", "outcome": "success"} in attrs
    assert {"target": "rabbitmq", "outcome": "failure"} in attrs
    assert {"target": "neo4j", "outcome": "success"} in attrs
    assert {"target": "postgres", "outcome": "success"} in attrs
    assert {"target": "loop", "outcome": "success"} in attrs


def test_recording_without_a_configured_provider_never_raises() -> None:
    """Every recording helper is safe to call before `setup_telemetry` runs."""
    console_telemetry.reset_instruments()
    console_telemetry.record_websocket_connection_delta(1)
    console_telemetry.record_poll_duration("neo4j", console_telemetry.POLL_OUTCOME_SUCCESS, 0.01)


class TestTelemetryRegression:
    """The service starts and serves normally when no OTLP endpoint is configured."""

    @pytest.fixture
    def app_client(
        self,
        mock_dashboard_config: DashboardConfig,
        dashboard_mock_amqp_connection: AsyncMock,
        dashboard_mock_neo4j_driver: MagicMock,
        dashboard_mock_httpx_client: MagicMock,
        dashboard_mock_psycopg_connect: AsyncMock,
    ) -> Iterator[TestClient]:
        """Boot the real app end-to-end, including the telemetry lifespan hooks.

        The background poll loop itself is not under test here (it is covered by
        ``TestDashboardApp`` and the domain-instrument tests above), so it is stubbed out —
        matching the existing ``test_lifespan_full_startup_and_shutdown`` pattern — to avoid an
        unawaited-coroutine warning from the mocked resilient-connection internals.
        """
        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientRabbitMQ", return_value=dashboard_mock_amqp_connection),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=dashboard_mock_neo4j_driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("httpx.AsyncClient") as mock_httpx_class,
            patch("dashboard.dashboard.DashboardApp.collect_metrics_loop", new_callable=AsyncMock),
        ):
            mock_httpx_class.return_value = dashboard_mock_httpx_client

            from dashboard.dashboard import app

            with TestClient(app) as client:
                yield client

    def test_service_starts_and_serves_health_without_an_otlp_endpoint(self, app_client: TestClient) -> None:
        """`OTEL_EXPORTER_OTLP_ENDPOINT` is unset (scrubbed by the autouse fixture); the
        lifespan's `setup_telemetry` / `instrument_fastapi_app` / `instrument_httpx` calls must
        not prevent normal startup or request handling.
        """
        response = app_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_metrics_route_is_removed(self, app_client: TestClient) -> None:
        """The hand-rolled Prometheus /metrics route no longer exists."""
        response = app_client.get("/metrics")
        assert response.status_code == 404
