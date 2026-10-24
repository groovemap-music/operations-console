"""Platform service health: the checks, their concurrency, the jobs, and what a down service raises."""

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from dashboard import dashboard as console
from dashboard import platform_health as ph
from dashboard.events import EventDetector


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client_over(handler: Any) -> Any:
    """A stand-in for ``httpx.AsyncClient`` answering from ``handler`` with the caller's timeout."""

    def factory(*_args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    return factory


def _probe(name: str) -> ph.Probe:
    return next(p for p in ph.build_probes("http://rabbitmq:15692") if p.name == name)


async def _check(probe: ph.Probe, handler: Any) -> ph.PlatformService:
    async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
        return await ph.check_probe(client, probe)


class TestProbes:
    def test_every_long_running_service_is_covered_by_group(self) -> None:
        by_group: dict[str, list[str]] = {}
        for probe in ph.build_probes("http://rabbitmq:15692"):
            by_group.setdefault(probe.group, []).append(probe.name)
        assert by_group == {
            "applications": ["catalog-api", "explore", "insights"],
            "data": ["valkey", "rabbitmq"],
            "observability": [
                "victoria-metrics",
                "victoria-traces",
                "otel-collector",
                "grafana",
                "postgres-exporter",
                "valkey-exporter",
                "cadvisor",
                "node-exporter",
            ],
        }

    def test_urls_are_the_ones_verified_against_the_deployment(self) -> None:
        urls = {p.name: p.target for p in ph.build_probes("http://rabbitmq:15692")}
        assert urls["catalog-api"] == "http://api:8005/health"  # the health port, not the API port 8004
        assert urls["explore"] == "http://explore:8007/health"
        assert urls["insights"] == "http://insights:8009/health"
        assert urls["valkey"] == "valkey:6379"
        assert urls["rabbitmq"] == "http://rabbitmq:15692/metrics/detailed?family=node_coarse_metrics"
        assert urls["victoria-metrics"] == "http://victoria-metrics:8428/health"
        assert urls["victoria-traces"] == "http://victoria-traces:10428/health"
        assert urls["otel-collector"] == "http://otel-collector:13133"
        assert urls["grafana"] == "http://grafana:3000/api/health"
        assert urls["postgres-exporter"] == "http://postgres-exporter:9187/"
        assert urls["valkey-exporter"] == "http://valkey-exporter:9121/metrics"
        assert urls["cadvisor"] == "http://cadvisor:8080/healthz"
        assert urls["node-exporter"] == "http://node-exporter:9100/"

    def test_the_consoles_own_settings_are_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VICTORIA_METRICS_URL", "http://vm.internal:9000/")
        monkeypatch.setenv("GRAFANA_API_URL", "http://grafana.internal:3001")
        urls = {p.name: p.target for p in ph.build_probes("http://broker:15692")}
        assert urls["victoria-metrics"] == "http://vm.internal:9000/health"
        assert urls["grafana"] == "http://grafana.internal:3001/api/health"
        assert urls["rabbitmq"].startswith("http://broker:15692/")

    def test_the_browser_facing_grafana_url_is_not_used_for_the_server_side_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GRAFANA_API_URL", raising=False)
        monkeypatch.setenv("GRAFANA_URL", "https://grafana.example.com")
        assert _probe("grafana").target == "http://grafana:3000/api/health"


class TestChecks:
    @pytest.mark.asyncio
    async def test_a_200_is_healthy(self) -> None:
        reading = await _check(_probe("catalog-api"), lambda _r: httpx.Response(200, json={"status": "healthy"}))
        assert (reading.status, reading.error, reading.group) == ("healthy", None, "applications")
        assert reading.last_seen is not None

    @pytest.mark.asyncio
    async def test_an_answer_that_is_not_a_200_is_unhealthy_with_the_code(self) -> None:
        reading = await _check(_probe("grafana"), lambda _r: httpx.Response(503))
        assert (reading.status, reading.error) == ("unhealthy", "HTTP 503")

    @pytest.mark.asyncio
    async def test_no_answer_is_unreachable(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        reading = await _check(_probe("explore"), refuse)
        assert (reading.status, reading.error, reading.last_seen) == ("unreachable", "connection refused", None)

    @pytest.mark.asyncio
    async def test_the_valkey_exporter_is_healthy_only_while_it_sees_valkey(self) -> None:
        probe = _probe("valkey-exporter")
        up = await _check(probe, lambda _r: httpx.Response(200, text="# HELP\nredis_up 1\nredis_uptime_in_seconds 59\n"))
        down = await _check(probe, lambda _r: httpx.Response(200, text="redis_up 0\n"))
        missing = await _check(probe, lambda _r: httpx.Response(200, text="process_cpu_seconds_total 1\n"))
        assert up.status == "healthy"
        assert (down.status, down.error) == ("unhealthy", "exporter cannot reach Valkey (redis_up 0)")
        assert (missing.status, missing.error) == ("unhealthy", "redis_up not reported")


async def _serve(reply: bytes) -> tuple[asyncio.Server, str]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, f"127.0.0.1:{server.sockets[0].getsockname()[1]}"


class TestValkeyPing:
    @staticmethod
    async def _ping(reply: bytes) -> ph.PlatformService:
        server, address = await _serve(reply)
        async with server:
            return await ph.check_probe(MagicMock(), ph.Probe("valkey", "data", address, ph.KIND_VALKEY_PING))

    @pytest.mark.asyncio
    async def test_pong_is_healthy(self) -> None:
        assert (await self._ping(b"+PONG\r\n")).status == "healthy"

    @pytest.mark.asyncio
    async def test_noauth_is_healthy_because_a_password_protected_server_is_still_up(self) -> None:
        reading = await self._ping(b"-NOAUTH Authentication required.\r\n")
        assert (reading.status, reading.error) == ("healthy", None)

    @pytest.mark.asyncio
    async def test_loading_is_unhealthy(self) -> None:
        reading = await self._ping(b"-LOADING Valkey is loading the dataset in memory\r\n")
        assert (reading.status, reading.error) == ("unhealthy", "unexpected reply to PING: -LOADING Valkey is loading the dataset i")

    @pytest.mark.asyncio
    async def test_any_other_error_is_unhealthy(self) -> None:
        reading = await self._ping(b"-ERR unknown command\r\n")
        assert (reading.status, reading.error) == ("unhealthy", "unexpected reply to PING: -ERR unknown command")

    @pytest.mark.asyncio
    async def test_a_closed_port_is_unreachable(self) -> None:
        server, address = await _serve(b"+PONG\r\n")
        server.close()
        await server.wait_closed()
        reading = await ph.check_probe(MagicMock(), ph.Probe("valkey", "data", address, ph.KIND_VALKEY_PING))
        assert reading.status == "unreachable"


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_one_slow_target_delays_the_poll_by_no_more_than_the_timeout(self) -> None:
        probes = [p for p in ph.build_probes("http://rabbitmq:15692") if p.kind != ph.KIND_VALKEY_PING]
        slow = "http://api:8005/health"

        async def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == slow:
                await asyncio.sleep(30)
            await asyncio.sleep(0.05)
            return httpx.Response(200, text="redis_up 1\n")

        with patch("httpx.AsyncClient", side_effect=_client_over(handler)):
            started = time.monotonic()
            readings = await ph.poll_platform(probes, timeout=0.5)
            elapsed = time.monotonic() - started

        assert elapsed < 1.5  # the timeout plus slack; sequential polling would take many multiples of it
        by_name = {r.name: r for r in readings}
        assert by_name["catalog-api"].status == "unreachable"
        assert by_name["catalog-api"].error == "no answer within 0.5s"
        assert [r.name for r in readings] == [p.name for p in probes]
        assert {r.status for name, r in by_name.items() if name != "catalog-api"} == {"healthy"}

    @pytest.mark.asyncio
    async def test_pipeline_services_are_polled_at_once_and_keep_their_order(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "graphinator":
                await asyncio.sleep(0.4)
            return httpx.Response(200, json={"status": "healthy"})

        with patch("dashboard.dashboard.get_config", return_value=Mock()):
            app = console.DashboardApp()
        targets = [
            ("extractor-discogs", "http://extractor-discogs:8000/health"),
            ("graphinator", "http://graphinator:8001/health"),
            ("tableinator", "http://tableinator:8002/health"),
        ]
        with patch("httpx.AsyncClient", side_effect=_client_over(handler)):
            started = time.monotonic()
            statuses = await app.get_service_statuses(targets)
            elapsed = time.monotonic() - started
        assert [s.name for s in statuses] == [name for name, _ in targets]
        assert elapsed < 0.4 * 2


class TestPipelineDown:
    @pytest.mark.asyncio
    async def test_a_pipeline_whose_services_are_all_unreachable_is_reported_as_down(self) -> None:
        """Regression: it used to be dropped from the reading, so an outage looked like an absent pipeline."""
        with patch("dashboard.dashboard.get_config", return_value=Mock()):
            app = console.DashboardApp()

        async def statuses(configs: list[tuple[str, str]]) -> list[console.ServiceStatus]:
            reachable = configs[0][0] == "extractor-discogs"
            return [
                console.ServiceStatus(
                    name=name, status="healthy" if reachable else "unknown", last_seen=None, current_task=None, progress=None, error=None
                )
                for name, _ in configs
            ]

        with (
            patch.object(app, "get_service_statuses", side_effect=statuses),
            patch.object(app, "get_queue_info", new_callable=AsyncMock, return_value=None),
            patch.object(app, "get_platform_health", new_callable=AsyncMock, return_value=[]),
            patch.object(app, "get_database_info", new_callable=AsyncMock, return_value=[]),
            patch.object(app, "get_job_status", new_callable=AsyncMock, return_value=[]),
        ):
            metrics = await app.collect_all_metrics()

        assert set(metrics.pipelines) == {"discogs", "musicbrainz"}
        assert metrics.pipelines["musicbrainz"].down is True
        assert {s.status for s in metrics.pipelines["musicbrainz"].services} == {"unknown"}
        assert metrics.pipelines["discogs"].down is False

    @pytest.mark.asyncio
    async def test_the_reading_carries_the_platform_with_the_datastores_in_the_data_group(self) -> None:
        with patch("dashboard.dashboard.get_config", return_value=Mock()):
            app = console.DashboardApp()
        seen = datetime.now(UTC)
        probed = [
            ph.PlatformService(name=n, group=g, status="healthy", last_seen=seen, error=None)
            for n, g in (("catalog-api", "applications"), ("valkey", "data"), ("grafana", "observability"))
        ]
        databases = [
            console.DatabaseInfo(name="PostgreSQL", status="healthy", connection_count=1, size=None, error=None),
            console.DatabaseInfo(name="Neo4j", status="unhealthy", connection_count=0, size=None, error="refused"),
        ]
        with (
            patch.object(app, "get_service_statuses", new_callable=AsyncMock, return_value=[]),
            patch.object(app, "get_queue_info", new_callable=AsyncMock, return_value=[]),
            patch.object(app, "get_platform_health", new_callable=AsyncMock, return_value=probed),
            patch.object(app, "get_database_info", new_callable=AsyncMock, return_value=databases),
            patch.object(app, "get_job_status", new_callable=AsyncMock, return_value=[]),
        ):
            metrics = await app.collect_all_metrics()
        assert [(s.name, s.status, s.source) for s in metrics.platform] == [
            ("catalog-api", "healthy", "probe"),
            ("postgres", "healthy", "datastore"),
            ("neo4j", "unhealthy", "datastore"),
            ("valkey", "healthy", "probe"),
            ("grafana", "healthy", "probe"),
        ]
        assert metrics.platform[2].error == "refused"


def _platform_reading(*services: tuple[str, str, str], at: int = 0) -> console.SystemMetrics:
    seen = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=at)
    return console.SystemMetrics(
        pipelines={"discogs": console.PipelineMetrics(services=[], queues=[])},
        databases=[],
        timestamp=seen,
        platform=[ph.PlatformService(name=n, group=g, status=s, last_seen=seen, error=None) for n, g, s in services],
    )


class TestPlatformEvents:
    def test_a_service_going_down_and_recovering_is_in_the_feed(self) -> None:
        detector = EventDetector()
        detector.observe(_platform_reading(("catalog-api", "applications", "healthy"), at=0))
        down = detector.observe(_platform_reading(("catalog-api", "applications", "unreachable"), at=2))
        up = detector.observe(_platform_reading(("catalog-api", "applications", "healthy"), at=4))
        assert [(e["kind"], e["level"], e["subject"], e["message"]) for e in down] == [
            ("service-down", "error", "catalog-api", "catalog-api is unreachable")
        ]
        assert [(e["kind"], e["level"], e["message"]) for e in up] == [("service-recovered", "info", "catalog-api is healthy")]
        assert [e["kind"] for e in detector.backlog()] == ["service-down", "service-recovered"]

    def test_unhealthy_to_unreachable_is_not_a_new_event(self) -> None:
        detector = EventDetector()
        detector.observe(_platform_reading(("valkey", "data", "healthy"), at=0))
        assert len(detector.observe(_platform_reading(("valkey", "data", "unhealthy"), at=2))) == 1
        assert detector.observe(_platform_reading(("valkey", "data", "unreachable"), at=4)) == []

    def test_an_observability_outage_is_a_warning(self) -> None:
        detector = EventDetector()
        detector.observe(_platform_reading(("grafana", "observability", "healthy"), at=0))
        [event] = detector.observe(_platform_reading(("grafana", "observability", "unhealthy"), at=2))
        assert event["level"] == "warning"

    def test_the_first_reading_is_a_baseline(self) -> None:
        detector = EventDetector()
        assert detector.observe(_platform_reading(("explore", "applications", "unreachable"))) == []

    def test_a_datastore_row_is_left_to_the_datastore_events(self) -> None:
        detector = EventDetector()
        row = ph.PlatformService(name="neo4j", group="data", status="healthy", last_seen=None, error=None, source="datastore")
        down = row.model_copy(update={"status": "unhealthy"})
        first = _platform_reading()
        first.platform = [row]
        second = _platform_reading(at=2)
        second.platform = [down]
        detector.observe(first)
        assert detector.observe(second) == []


class _Cursor:
    def __init__(self, row: tuple[Any, ...] | None, error: Exception | None) -> None:
        self.row, self.error, self.sql = row, error, None

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, sql: str) -> None:
        self.sql = sql
        if self.error:
            raise self.error

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


def _app_reading(row: tuple[Any, ...] | None = None, error: Exception | None = None) -> tuple[console.DashboardApp, _Cursor]:
    with patch("dashboard.dashboard.get_config", return_value=Mock()):
        app = console.DashboardApp()
    cursor = _Cursor(row, error)
    connection = MagicMock()
    connection.cursor = lambda: cursor
    app.postgres_conn = MagicMock()
    app.postgres_conn.get_connection = AsyncMock(return_value=connection)
    return app, cursor


class TestJobs:
    @pytest.mark.asyncio
    async def test_the_embeddings_job_shows_its_last_load_from_the_durable_record(self) -> None:
        loaded = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
        app, cursor = _app_reading((("discogs-2026-09"), date(2026, 9, 1), loaded))
        jobs = {job.name: job for job in await app.get_job_status()}
        assert cursor.sql == ph.LAST_EMBEDDINGS_LOAD_SQL
        assert jobs["embeddings"].model_dump(include={"monitored", "outcome", "last_run", "dump_id"}) == {
            "monitored": True,
            "outcome": "loaded",
            "last_run": loaded,
            "dump_id": "discogs-2026-09",
        }
        assert "discogs-2026-09 (2026-09-01)" in jobs["embeddings"].detail

    @pytest.mark.asyncio
    async def test_jobs_with_no_readable_record_say_so(self) -> None:
        app, _ = _app_reading(error=RuntimeError('permission denied for table "artist_embeddings"'))
        jobs = {job.name: job for job in await app.get_job_status()}
        for name in ("schema-init", "rabbitmq-dlq-policy-init", "embeddings"):
            assert (jobs[name].monitored, jobs[name].detail) == (False, "not monitored here")

    @pytest.mark.asyncio
    async def test_an_empty_table_is_monitored_but_has_no_run(self) -> None:
        app, _ = _app_reading(None)
        embeddings = next(job for job in await app.get_job_status() if job.name == "embeddings")
        assert (embeddings.monitored, embeddings.outcome, embeddings.last_run) == (True, None, None)

    @pytest.mark.asyncio
    async def test_the_record_is_not_read_again_within_the_cache_window(self) -> None:
        app, _ = _app_reading(None)
        await app.get_job_status()
        await app.get_job_status()
        assert app.postgres_conn.get_connection.await_count == 1  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_without_a_database_connection_the_embeddings_job_is_not_monitored(self) -> None:
        with patch("dashboard.dashboard.get_config", return_value=Mock()):
            app = console.DashboardApp()
        embeddings = next(job for job in await app.get_job_status() if job.name == "embeddings")
        assert embeddings.monitored is False
