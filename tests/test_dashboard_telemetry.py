"""Behavioral tests for the operations-console domain OpenTelemetry signals.

Mirrors groovemap-runtime's own `tests/test_runtime_metrics.py` pattern: an in-memory
`SdkMeterProvider` and an in-memory `TracerProvider` are installed directly into
`common.telemetry`'s module state so the console's lazily-built instruments and its
`console.poll {target}` spans bind to them, and what was recorded is read back by name.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from common import instrument_httpx, telemetry
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from dashboard import telemetry as console_telemetry
from dashboard.dashboard import SystemMetrics


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.metrics.export import Metric
    from opentelemetry.sdk.trace import ReadableSpan

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


class SpanCollector:
    """An in-memory tracer provider whose finished spans can be read back by name."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.provider = SdkTracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def spans(self) -> tuple[ReadableSpan, ...]:
        """Return every span that has finished so far."""
        return self.exporter.get_finished_spans()

    def named(self, name: str) -> list[ReadableSpan]:
        """Return the finished spans carrying exactly `name`."""
        return [span for span in self.spans() if span.name == name]

    def only(self, name: str) -> ReadableSpan:
        """Return the one finished span named `name`, asserting there is exactly one."""
        matching = self.named(name)
        assert len(matching) == 1, f"expected exactly one {name!r} span, saw {[span.name for span in self.spans()]}"
        return matching[0]

    def console_spans(self) -> list[ReadableSpan]:
        """Return only the spans the console itself opened, ignoring the instrumentors'."""
        return [
            span
            for span in self.spans()
            if span.instrumentation_scope is not None and span.instrumentation_scope.name == console_telemetry.INSTRUMENTATION_SCOPE
        ]


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[SpanCollector]:
    """Record every span the console opens into an in-memory exporter.

    `poll_span` resolves its tracer through `common.telemetry.tracer_provider()` on each call,
    so installing the collector's provider into `common.telemetry`'s module state is enough to
    capture spans without going near the process-wide OpenTelemetry provider.
    """
    collector = SpanCollector()
    monkeypatch.setattr(telemetry, "_tracer_provider", collector.provider)
    yield collector
    monkeypatch.setattr(telemetry, "_tracer_provider", None)


@pytest.fixture
def console_app(mock_dashboard_config: DashboardConfig) -> Any:
    """A DashboardApp built against the mock config, with no connections opened."""
    from dashboard.dashboard import DashboardApp

    with patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config):
        return DashboardApp()


# Bound before any test patches `httpx.AsyncClient`, so the factory below builds a real client
# instead of calling its own patch back into itself.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _instrumented_client_factory(handler: Any) -> Any:
    """Return a stand-in for `httpx.AsyncClient` that answers from `handler`, instrumented.

    The console builds its own client inside `get_service_statuses`, and the httpx instrumentor
    only wraps a real network transport, so the test swaps in a `MockTransport` client and
    instruments that one client through the same `common.instrument_httpx` entry point the
    lifespan calls process-wide. Only the socket is fake: the span the instrumentor emits, and
    the context it picks its parent from, are the production ones.
    """

    def factory(*_args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 5.0))
        instrument_httpx(client)
        return client

    return factory


class TestPollSpans:
    """`console.poll {target}` — one root span per polled target, the poll's work inside it."""

    @pytest.mark.asyncio
    async def test_service_poll_opens_a_root_span_with_the_http_call_as_its_child(
        self, console_app: Any, spans: SpanCollector, collector: Collector
    ) -> None:
        """A healthy service poll: one root span, attributes target and outcome, httpx below it."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "healthy"})

        with patch("httpx.AsyncClient", side_effect=_instrumented_client_factory(handler)):
            statuses = await console_app.get_service_statuses([("extractor-discogs", "http://extractor-discogs:8000/health")])

        assert [status.status for status in statuses] == ["healthy"]

        poll = spans.only("console.poll extractor-discogs")
        assert poll.parent is None, "console.poll is a domain root span"
        assert dict(poll.attributes or {}) == {"target": "extractor-discogs", "outcome": "success"}
        assert poll.status.status_code is StatusCode.UNSET

        client_spans = [span for span in spans.spans() if span.kind is SpanKind.CLIENT]
        assert len(client_spans) == 1
        assert client_spans[0].parent is not None
        assert client_spans[0].parent.span_id == poll.context.span_id

        # The span and the histogram report the same outcome for the same poll.
        assert {"target": "extractor-discogs", "outcome": "success"} in collector.attributes(console_telemetry.POLL_DURATION)

    @pytest.mark.asyncio
    async def test_a_poll_that_is_answered_badly_is_a_failure_without_an_error_status(self, console_app: Any, spans: SpanCollector) -> None:
        """A reachable service answering 503 is a handled result, not a console error."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        with patch("httpx.AsyncClient", side_effect=_instrumented_client_factory(handler)):
            statuses = await console_app.get_service_statuses([("graphinator", "http://graphinator:8000/health")])

        assert [status.status for status in statuses] == ["unhealthy"]
        poll = spans.only("console.poll graphinator")
        assert dict(poll.attributes or {}) == {"target": "graphinator", "outcome": "failure"}
        assert poll.status.status_code is StatusCode.UNSET
        assert poll.status.description is None
        assert poll.events == ()

    @pytest.mark.asyncio
    async def test_an_unreachable_service_sets_error_status_with_the_type_and_nothing_else(self, console_app: Any, spans: SpanCollector) -> None:
        """A refused connection is a real error: status ERROR, error.type, no message anywhere."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused, secret-looking detail", request=request)

        with patch("httpx.AsyncClient", side_effect=_instrumented_client_factory(handler)):
            statuses = await console_app.get_service_statuses([("tableinator", "http://tableinator:8000/health")])

        assert [status.status for status in statuses] == ["unknown"]
        poll = spans.only("console.poll tableinator")
        assert dict(poll.attributes or {}) == {"target": "tableinator", "outcome": "failure", "error.type": "ConnectError"}
        assert poll.status.status_code is StatusCode.ERROR
        assert poll.status.description is None, "an error is reported as error.type only, never as a message"
        assert poll.events == (), "no span event may carry the exception payload"

    @pytest.mark.asyncio
    async def test_each_polled_service_gets_its_own_root_span(self, console_app: Any, spans: SpanCollector) -> None:
        """Three services polled through one client are three sibling roots, never nested."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "healthy"})

        targets = [("extractor-discogs", "http://a:8000/health"), ("graphinator", "http://b:8000/health"), ("tableinator", "http://c:8000/health")]
        with patch("httpx.AsyncClient", side_effect=_instrumented_client_factory(handler)):
            await console_app.get_service_statuses(targets)

        polls = spans.console_spans()
        assert [span.name for span in polls] == ["console.poll extractor-discogs", "console.poll graphinator", "console.poll tableinator"]
        assert all(span.parent is None for span in polls)

    @pytest.mark.asyncio
    async def test_a_poll_stays_a_root_span_when_a_request_handler_triggered_it(self, console_app: Any, spans: SpanCollector) -> None:
        """`/api/services` runs inside the FastAPI SERVER span; the poll must not hang off it."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "healthy"})

        tracer = spans.provider.get_tracer("test.http.server")
        with (
            patch("httpx.AsyncClient", side_effect=_instrumented_client_factory(handler)),
            tracer.start_as_current_span("GET /api/services", kind=SpanKind.SERVER),
        ):
            await console_app.get_service_statuses([("extractor-discogs", "http://extractor-discogs:8000/health")])

        assert spans.only("console.poll extractor-discogs").parent is None

    @pytest.mark.asyncio
    async def test_neo4j_and_postgres_polls_each_open_one_span(
        self, console_app: Any, spans: SpanCollector, dashboard_mock_neo4j_driver: MagicMock
    ) -> None:
        """The two database targets are polled under their own root spans."""
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(10,))
        connection = MagicMock()
        connection.cursor = MagicMock(return_value=cursor)

        console_app.neo4j_driver = dashboard_mock_neo4j_driver
        console_app.postgres_conn = AsyncMock()
        console_app.postgres_conn.get_connection = AsyncMock(return_value=connection)

        await console_app.get_database_info()

        assert sorted(span.name for span in spans.console_spans()) == ["console.poll neo4j", "console.poll postgres"]
        for name in ("console.poll neo4j", "console.poll postgres"):
            assert spans.only(name).parent is None

    @pytest.mark.asyncio
    async def test_the_whole_collection_cycle_is_not_wrapped_in_a_span(self, console_app: Any, spans: SpanCollector, collector: Collector) -> None:
        """The reserved "loop" target stays a metric only.

        A `console.poll loop` span would enclose every real poll of the cycle and demote all of
        them from roots to children, which is exactly what the span conventions forbid, so the
        end-to-end cycle duration is reported through the histogram alone.
        """
        cycles = 0

        async def one_cycle_then_stop() -> Any:
            nonlocal cycles
            cycles += 1
            if cycles > 1:
                raise asyncio.CancelledError
            return SystemMetrics(pipelines={}, databases=[], timestamp=datetime.now(UTC))

        with (
            patch.object(console_app, "collect_all_metrics", side_effect=one_cycle_then_stop),
            patch.object(console_app, "broadcast_metrics", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await console_app.collect_metrics_loop()

        assert {"target": "loop", "outcome": "success"} in collector.attributes(console_telemetry.POLL_DURATION)
        assert spans.named("console.poll loop") == []
        assert spans.console_spans() == []


class TestLifespanTelemetryBracket:
    """setup_telemetry → instrumentors → event-loop monitor, and shutdown_telemetry on exit."""

    @pytest.fixture
    def booted(
        self,
        mock_dashboard_config: DashboardConfig,
        dashboard_mock_amqp_connection: AsyncMock,
        dashboard_mock_neo4j_driver: MagicMock,
        dashboard_mock_httpx_client: MagicMock,
        dashboard_mock_psycopg_connect: AsyncMock,
    ) -> Iterator[dict[str, Any]]:
        """Boot the real app through its real lifespan with the telemetry calls recorded."""
        order: list[str] = []
        recorded: dict[str, Any] = {"order": order}

        def record(name: str, result: Any = None) -> Any:
            def hook(*_args: Any, **_kwargs: Any) -> Any:
                order.append(name)
                return result

            return hook

        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientRabbitMQ", return_value=dashboard_mock_amqp_connection),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=dashboard_mock_neo4j_driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("httpx.AsyncClient", return_value=dashboard_mock_httpx_client),
            patch("dashboard.dashboard.DashboardApp.collect_metrics_loop", new_callable=AsyncMock),
            patch("dashboard.dashboard.setup_telemetry", side_effect=record("setup_telemetry")),
            patch("dashboard.dashboard.instrument_fastapi_app", side_effect=record("instrument_fastapi_app", True)),
            patch("dashboard.dashboard.instrument_httpx", side_effect=record("instrument_httpx", True)),
            patch("dashboard.dashboard.start_event_loop_monitor", side_effect=record("start_event_loop_monitor")) as monitor,
            patch("dashboard.dashboard.shutdown_telemetry", side_effect=record("shutdown_telemetry")) as shutdown,
        ):
            from dashboard.dashboard import app

            recorded["start_event_loop_monitor"] = monitor
            recorded["shutdown_telemetry"] = shutdown
            with TestClient(app) as client:
                recorded["client"] = client
                yield recorded

    def test_the_event_loop_monitor_starts_once_after_telemetry_is_configured(self, booted: dict[str, Any]) -> None:
        """It samples the loop the console serves on, so it can only start inside the lifespan."""
        monitor = booted["start_event_loop_monitor"]
        monitor.assert_called_once_with()
        order = booted["order"]
        assert order.index("setup_telemetry") < order.index("start_event_loop_monitor")

    def test_both_providers_are_still_flushed_on_exit(self, booted: dict[str, Any]) -> None:
        """shutdown_telemetry force-flushes traces and metrics; the lifespan must still call it."""
        shutdown = booted["shutdown_telemetry"]
        assert shutdown.call_count == 0, "shutdown belongs on the way out, not during startup"
        booted["client"].__exit__(None, None, None)
        shutdown.assert_called_once_with()
        assert booted["order"][-1] == "shutdown_telemetry"


class TestWebsocketFramesAreNotTraced:
    """A dashboard client holds one socket for hours; a span per frame would swamp the trace store."""

    @pytest.fixture
    def ws_client(
        self,
        mock_dashboard_config: DashboardConfig,
        dashboard_mock_amqp_connection: AsyncMock,
        dashboard_mock_neo4j_driver: MagicMock,
        dashboard_mock_httpx_client: MagicMock,
        dashboard_mock_psycopg_connect: AsyncMock,
    ) -> Iterator[TestClient]:
        """Boot the real app with its background poll loop stubbed out."""
        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientRabbitMQ", return_value=dashboard_mock_amqp_connection),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=dashboard_mock_neo4j_driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("httpx.AsyncClient", return_value=dashboard_mock_httpx_client),
            patch("dashboard.dashboard.DashboardApp.collect_metrics_loop", new_callable=AsyncMock),
        ):
            from dashboard.dashboard import app

            with TestClient(app) as client:
                yield client

    def test_no_span_is_opened_per_websocket_frame(self, ws_client: TestClient, spans: SpanCollector) -> None:
        """A metrics push out and twenty frames in leave the console's span scope empty."""
        import dashboard.dashboard as module

        module.dashboard.latest_metrics = SystemMetrics(pipelines={}, databases=[], timestamp=datetime.now(UTC))

        with ws_client.websocket_connect("/ws") as websocket:
            assert websocket.receive_json()["type"] == "metrics_update"
            for _ in range(20):
                websocket.send_text("ping")

        assert spans.console_spans() == []


class TestTracesDisabledWithEndpointSet:
    """OTEL_TRACES_EXPORTER=none turns tracing off on its own; metrics keep exporting."""

    @pytest.mark.asyncio
    async def test_metrics_flow_and_no_span_is_recorded(self, monkeypatch: pytest.MonkeyPatch, console_app: Any) -> None:
        """Port 1 is never listening, so the OTLP exporter fails fast instead of holding the
        shutdown flush open; nothing here depends on an export succeeding.
        """
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
        monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "healthy"})

        telemetry.shutdown_telemetry(timeout_s=0.1)
        console_telemetry.reset_instruments()
        try:
            provider = telemetry.setup_telemetry("dashboard")

            # Metrics half: a real SDK provider is installed, so measurements are exported.
            assert provider is not None
            assert telemetry._sdk_provider is not None

            # Tracing half: no SDK tracer provider exists at all, so the poll span is not even
            # recorded, let alone exported.
            assert telemetry._sdk_tracer_provider is None
            with console_telemetry.poll_span("neo4j"):
                current = trace.get_current_span()
                assert current.is_recording() is False

            with patch("httpx.AsyncClient", side_effect=_instrumented_client_factory(handler)):
                statuses = await console_app.get_service_statuses([("extractor-discogs", "http://extractor-discogs:8000/health")])
            assert [status.status for status in statuses] == ["healthy"]
        finally:
            telemetry.shutdown_telemetry(timeout_s=0.1)
            console_telemetry.reset_instruments()


# ---------------------------------------------------------------------------
# gm-operations-console-wmi.2 — the Neo4j observable gauges
# ---------------------------------------------------------------------------


class FakeNeo4jResult:
    """The rows one fake query answers with."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        """Return the rows, mirroring the async driver's `AsyncResult.data()`."""
        return self._rows


class FakeNeo4jSession:
    """A session that answers each query from a scripted table and counts what it was asked."""

    def __init__(self, driver: FakeNeo4jDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> FakeNeo4jSession:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def run(self, query: str) -> FakeNeo4jResult:
        """Answer one query, applying whatever delay or failure the driver was scripted with."""
        self._driver.queries.append(query)
        if self._driver.delay_s:
            await asyncio.sleep(self._driver.delay_s)
        if self._driver.fails:
            raise RuntimeError("Neo4j is unreachable at bolt://neo4j:7687")
        if query.startswith("CALL dbms.queryJmx"):
            if not self._driver.jmx_available:
                raise RuntimeError("There is no procedure with the name `dbms.queryJmx`")
            return FakeNeo4jResult(self._driver.jmx_rows)
        if query.startswith("SHOW TRANSACTIONS"):
            return FakeNeo4jResult([{"count": self._driver.transactions}])
        if query.startswith("MATCH ()-["):
            return FakeNeo4jResult([{"name": name, "count": count} for name, count in self._driver.relationships.items()])
        return FakeNeo4jResult([{"name": name, "count": count} for name, count in self._driver.nodes.items()])


class FakeNeo4jDriver:
    """A scriptable stand-in for `AsyncResilientNeo4jDriver` that opens no connection."""

    def __init__(
        self,
        *,
        nodes: dict[str, int] | None = None,
        relationships: dict[str, int] | None = None,
        transactions: int = 3,
        jmx_rows: list[dict[str, Any]] | None = None,
        jmx_available: bool = True,
        delay_s: float = 0.0,
        fails: bool = False,
    ) -> None:
        self.nodes = nodes if nodes is not None else {label: index + 1 for index, label in enumerate(console_telemetry.NEO4J_NODE_LABELS)}
        self.relationships = (
            relationships
            if relationships is not None
            else {name: (index + 1) * 10 for index, name in enumerate(console_telemetry.NEO4J_RELATIONSHIP_TYPES)}
        )
        self.transactions = transactions
        self.jmx_available = jmx_available
        self.jmx_rows = jmx_rows if jmx_rows is not None else [{"attributes": {"TotalStoreSize": {"value": 4096}, "NodeStore": {"value": 512}}}]
        self.delay_s = delay_s
        self.fails = fails
        self.sessions = 0
        self.queries: list[str] = []

    def session(self) -> FakeNeo4jSession:
        """Open one scripted session."""
        self.sessions += 1
        return FakeNeo4jSession(self)


@pytest.fixture
def neo4j_gauges(collector: Collector) -> Iterator[Collector]:
    """Register the Neo4j gauges against the in-memory provider and clear them afterwards."""
    console_telemetry.reset_neo4j_gauges()
    yield collector
    console_telemetry.reset_neo4j_gauges()


async def _collect(collector: Collector) -> dict[str, Any]:
    """Collect the in-memory reader from a worker thread, as the real exporter does.

    The gauge callbacks block their own thread waiting on the console's event loop, so a
    collection driven from inside that loop would deadlock. The real periodic reader runs on
    its own thread; this runs on one too.
    """
    return await asyncio.to_thread(collector.metrics)


class TestNeo4jSchemaSets:
    """The label and type sets the gauges report are pinned to the schema, not discovered."""

    def test_node_labels_are_the_closed_schema_set(self) -> None:
        """Ten labels; database-schema owns them, and a change there must fail here first."""
        assert console_telemetry.NEO4J_NODE_LABELS == (
            "Artist",
            "Genre",
            "Label",
            "Master",
            "MediaFamily",
            "Medium",
            "Person",
            "Release",
            "Style",
            "User",
        )

    def test_relationship_types_are_the_closed_schema_set(self) -> None:
        """The twenty-one edge types the graph schema defines, Discogs- and MusicBrainz-sourced."""
        assert set(console_telemetry.NEO4J_RELATIONSHIP_TYPES) == {
            "ALIAS_OF",
            "BY",
            "COLLABORATED_WITH",
            "COLLECTED",
            "CREDITED_ON",
            "DERIVED_FROM",
            "FOUNDED",
            "IN_FAMILY",
            "IS",
            "ISSUED_ON",
            "MEMBER_OF",
            "ON",
            "PART_OF",
            "RENAMED_TO",
            "SAME_AS",
            "SUBGROUP_OF",
            "SUBLABEL_OF",
            "SUPPORTED",
            "TAUGHT",
            "TRIBUTE_TO",
            "WANTS",
        }
        assert len(console_telemetry.NEO4J_RELATIONSHIP_TYPES) == 21

    def test_every_count_query_is_a_count_store_query(self) -> None:
        """No count may scan the graph: at 134 million edges a scan would never finish."""
        node_branches = console_telemetry.NEO4J_NODE_COUNT_QUERY.split("\nUNION ALL\n")
        assert node_branches == [f"MATCH (n:{label}) RETURN '{label}' AS name, count(n) AS count" for label in console_telemetry.NEO4J_NODE_LABELS]
        rel_branches = console_telemetry.NEO4J_RELATIONSHIP_COUNT_QUERY.split("\nUNION ALL\n")
        assert rel_branches == [
            f"MATCH ()-[r:{name}]->() RETURN '{name}' AS name, count(r) AS count" for name in console_telemetry.NEO4J_RELATIONSHIP_TYPES
        ]

    def test_the_query_budget_is_two_seconds(self) -> None:
        """The bound the slow case relies on, stated once so the tests can shorten it safely."""
        assert console_telemetry.NEO4J_QUERY_TIMEOUT_S == 2.0


class TestNeo4jGaugesHealthy:
    """A store that answers: every gauge reports, and one refresh serves the whole cycle."""

    @pytest.mark.asyncio
    async def test_every_gauge_reports_from_a_single_shared_refresh(self, neo4j_gauges: Collector) -> None:
        """Five callbacks, one session, one set of queries."""
        driver = FakeNeo4jDriver()
        console_telemetry.register_neo4j_gauges(driver)

        await _collect(neo4j_gauges)

        assert neo4j_gauges.value(console_telemetry.NEO4J_UP) == 1
        assert driver.sessions == 1, "the five callbacks share one refresh"
        assert len(driver.queries) == 4, "nodes, relationships, transactions, store sizes"

        nodes = {dict(point.attributes)["label"]: point.value for point in neo4j_gauges.points(console_telemetry.NEO4J_NODES)}
        assert nodes == driver.nodes
        relationships = {dict(point.attributes)["type"]: point.value for point in neo4j_gauges.points(console_telemetry.NEO4J_RELATIONSHIPS)}
        assert relationships == driver.relationships
        assert neo4j_gauges.value(console_telemetry.NEO4J_TRANSACTIONS_ACTIVE) == 3
        stores = {dict(point.attributes)["store"]: point.value for point in neo4j_gauges.points(console_telemetry.NEO4J_STORE_SIZE_BYTES)}
        assert stores == {"total": 4096, "node": 512}

    @pytest.mark.asyncio
    async def test_a_second_collection_within_the_interval_reuses_the_snapshot(
        self, neo4j_gauges: Collector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "At most once per export interval" is the point: a spare collection costs no query."""
        monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")
        driver = FakeNeo4jDriver()
        console_telemetry.register_neo4j_gauges(driver)

        await _collect(neo4j_gauges)
        await _collect(neo4j_gauges)

        assert driver.sessions == 1

    @pytest.mark.asyncio
    async def test_a_collection_after_the_interval_refreshes(self, neo4j_gauges: Collector, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once the snapshot is older than half an interval the next callback refetches it."""
        monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "2000")
        driver = FakeNeo4jDriver()
        console_telemetry.register_neo4j_gauges(driver)

        await _collect(neo4j_gauges)
        moved_on = time.monotonic() + 5.0
        monkeypatch.setattr(console_telemetry, "_monotonic", lambda: moved_on)
        await _collect(neo4j_gauges)

        assert driver.sessions == 2

    @pytest.mark.asyncio
    async def test_store_sizes_are_omitted_when_the_jmx_procedure_is_absent(self, neo4j_gauges: Collector) -> None:
        """A Neo4j without dbms.queryJmx is healthy; only its store sizes are unknown."""
        driver = FakeNeo4jDriver(jmx_available=False)
        console_telemetry.register_neo4j_gauges(driver)

        await _collect(neo4j_gauges)

        assert neo4j_gauges.value(console_telemetry.NEO4J_UP) == 1
        assert neo4j_gauges.points(console_telemetry.NEO4J_STORE_SIZE_BYTES) == []
        assert len(neo4j_gauges.points(console_telemetry.NEO4J_NODES)) == len(console_telemetry.NEO4J_NODE_LABELS)


class TestNeo4jGaugesUnavailable:
    """A store that is down or too slow reports up=0 and nothing else — never a zero count."""

    @pytest.mark.asyncio
    async def test_a_failing_store_reports_only_up_zero(self, neo4j_gauges: Collector) -> None:
        """Zeros here would read as "the graph emptied", which is a very different incident."""
        console_telemetry.register_neo4j_gauges(FakeNeo4jDriver(fails=True))

        await _collect(neo4j_gauges)

        assert neo4j_gauges.value(console_telemetry.NEO4J_UP) == 0
        assert neo4j_gauges.points(console_telemetry.NEO4J_NODES) == []
        assert neo4j_gauges.points(console_telemetry.NEO4J_RELATIONSHIPS) == []
        assert neo4j_gauges.points(console_telemetry.NEO4J_TRANSACTIONS_ACTIVE) == []
        assert neo4j_gauges.points(console_telemetry.NEO4J_STORE_SIZE_BYTES) == []

    @pytest.mark.asyncio
    async def test_a_slow_store_is_cut_off_and_reported_down(self, neo4j_gauges: Collector, monkeypatch: pytest.MonkeyPatch) -> None:
        """The real budget is two seconds; shortened here so the suite does not wait for it."""
        monkeypatch.setattr(console_telemetry, "NEO4J_QUERY_TIMEOUT_S", 0.05)
        driver = FakeNeo4jDriver(delay_s=1.0)
        console_telemetry.register_neo4j_gauges(driver)

        started = time.monotonic()
        await _collect(neo4j_gauges)
        elapsed = time.monotonic() - started

        assert neo4j_gauges.value(console_telemetry.NEO4J_UP) == 0
        assert neo4j_gauges.points(console_telemetry.NEO4J_NODES) == []
        assert elapsed < 1.0, "a slow store must not hold the export open for its own timeout"

    @pytest.mark.asyncio
    async def test_the_callbacks_report_nothing_once_the_driver_is_detached(self, neo4j_gauges: Collector) -> None:
        """After shutdown there is no console to be up or down, so nothing is claimed."""
        console_telemetry.register_neo4j_gauges(FakeNeo4jDriver())
        await _collect(neo4j_gauges)
        console_telemetry.reset_neo4j_gauges()

        await _collect(neo4j_gauges)

        assert neo4j_gauges.points(console_telemetry.NEO4J_UP) == []

    @pytest.mark.asyncio
    async def test_a_closed_event_loop_never_raises_into_the_exporter(self, neo4j_gauges: Collector, monkeypatch: pytest.MonkeyPatch) -> None:
        """The loop can go away between two collections; the callbacks still have to return."""
        console_telemetry.register_neo4j_gauges(FakeNeo4jDriver())
        await _collect(neo4j_gauges)
        source = console_telemetry._neo4j_source
        assert source is not None

        dead = asyncio.new_event_loop()
        dead.close()
        source._loop = dead
        moved_on = time.monotonic() + 3600.0
        monkeypatch.setattr(console_telemetry, "_monotonic", lambda: moved_on)

        await _collect(neo4j_gauges)

        assert neo4j_gauges.value(console_telemetry.NEO4J_UP) == 0


class TestNeo4jGaugeRegistration:
    """Registration is part of the console's startup and detaches on the way out."""

    @pytest.mark.asyncio
    async def test_startup_registers_the_gauges_against_the_live_driver(
        self, mock_dashboard_config: DashboardConfig, dashboard_mock_amqp_connection: AsyncMock, dashboard_mock_psycopg_connect: AsyncMock
    ) -> None:
        """The driver the gauges read is the one the console just built, not a second one."""
        from dashboard.dashboard import DashboardApp

        driver = FakeNeo4jDriver()
        console_telemetry.reset_neo4j_gauges()
        with (
            patch("dashboard.dashboard.get_config", return_value=mock_dashboard_config),
            patch("dashboard.dashboard.AsyncResilientRabbitMQ", return_value=dashboard_mock_amqp_connection),
            patch("dashboard.dashboard.AsyncResilientNeo4jDriver", return_value=driver),
            patch("dashboard.dashboard.AsyncResilientPostgreSQL", return_value=dashboard_mock_psycopg_connect),
            patch("dashboard.dashboard.DashboardApp.collect_metrics_loop", new_callable=AsyncMock),
        ):
            app = DashboardApp()
            await app.startup()
            try:
                source = console_telemetry._neo4j_source
                assert source is not None
                assert source._driver is driver
            finally:
                app.update_task = None
                app.rabbitmq = None
                app.postgres_conn = None
                driver.close = AsyncMock()
                await app.shutdown()

        assert console_telemetry._neo4j_source is None, "shutdown detaches the gauges before closing the driver"
