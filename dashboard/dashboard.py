#!/usr/bin/env python3
"""Operations console for monitoring GrooveMap components."""

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import AsyncGenerator  # noqa: TC003  # FastAPI resolves lifespan annotations at runtime.
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import orjson
import uvicorn
from common import (
    AsyncResilientNeo4jDriver,
    AsyncResilientPostgreSQL,
    instrument_fastapi_app,
    instrument_httpx,
    neo4j_security_kwargs,
    parse_postgres_host_port,
    setup_logging,
    setup_telemetry,
    shutdown_telemetry,
    start_event_loop_monitor,
)
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dashboard import telemetry as console_telemetry
from dashboard.admin_proxy import configure as configure_admin_proxy
from dashboard.admin_proxy import public_router as admin_public_router
from dashboard.admin_proxy import router as admin_router
from dashboard.catalog_contract import DISCOGS_EXCHANGE_PREFIX, MUSICBRAINZ_EXCHANGE_PREFIX
from dashboard.config import get_config
from dashboard.deep_links import router as deep_links_router
from dashboard.events import EventDetector
from dashboard.grafana_alerts import router as grafana_alerts_router
from dashboard.history import router as history_router
from dashboard.platform_health import (
    JOB_CACHE_SECONDS,
    LAST_EMBEDDINGS_LOAD_SQL,
    JobStatus,
    PlatformService,
    build_probes,
    datastore_services,
    embeddings_job,
    embeddings_unreadable,
    poll_platform,
    unmonitored_jobs,
)
from dashboard.progress import configure as configure_progress
from dashboard.progress import router as progress_router
from dashboard.queue_metrics import RateTracker, parse_queue_samples, scrape_params
from dashboard.session_auth import (
    WS_CLOSE_UNAUTHENTICATED,
    WS_CLOSE_UNAVAILABLE,
    WS_TICKET_SCOPE_KEY,
    AdminSession,
    SessionRejected,
    cache_seconds_from_env,
    install_ticket_redaction,
    require_session,
    session_rejected_handler,
    take_ws_ticket,
)
from dashboard.session_auth import gate as session_gate
from dashboard.topology import build_topology


# Suppress per-request httpx INFO logs (health checks every 2s are too noisy)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

SOURCE_REPOSITORY = "https://github.com/groovemap-music/operations-console"


def source_url(revision: str | None) -> str:
    """Return corresponding source when given an exact Git commit."""
    if revision is not None and re.fullmatch(r"[0-9a-f]{40}", revision):
        return f"{SOURCE_REPOSITORY}/tree/{revision}"
    return SOURCE_REPOSITORY


SOURCE_URL = source_url(os.environ.get("GROOVEMAP_SOURCE_REVISION"))

STARTUP_BANNER = r"""
                         GrooveMap operations-console
                        _   _                                     _
 ___ _ __  ___ _ _ __ _| |_(_)___ _ _  ______ __ ___ _ _  ___ ___| |___
/ _ \ '_ \/ -_) '_/ _` |  _| / _ \ ' \(_-<___/ _/ _ \ ' \(_-</ _ \ / -_)
\___/ .__/\___|_| \__,_|\__|_\___/_||_/__/   \__\___/_||_/__/\___/_\___|
    |_|
""".strip("\n")

# CORS origins configurable via environment variable (comma-separated list)
_cors_origins_raw = os.environ.get("CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()] if _cors_origins_raw else None


# Per-client cap on how long broadcast_metrics waits on a single websocket.send_text().
# send_text applies TCP backpressure and can block indefinitely for a stalled client
# (sleeping laptop, zero-window peer); this bounds the blast radius to that one client.
_WS_SEND_TIMEOUT_SECONDS = 5.0

# How often an open WebSocket re-validates the session it was opened with. Matches the
# session cache's 30-second ceiling, so a revoked session loses its live feed within about a
# minute even when no browser tab is left polling to notice.
_WS_SESSION_RECHECK_SECONDS = 30.0

# Every route that returns pipeline, queue, or datastore data requires a live admin session.
_SESSION_REQUIRED = [Depends(require_session)]


class ServiceStatus(BaseModel):
    """Model for service status information."""

    name: str
    status: str  # healthy, unhealthy, unknown, starting, extracting
    last_seen: datetime | None
    current_task: str | None
    progress: float | None  # 0.0 to 1.0
    error: str | None
    extraction_progress: dict[str, int] | None = None  # extractor only: per-type counts + total
    last_extraction_time: dict[str, float | None] | None = None  # extractor only: seconds since last activity


class QueueInfo(BaseModel):
    """Model for RabbitMQ queue information.

    The rates are computed from counter deltas between polls; a queue with no computable rate
    (first sample, counter reset, missed poll) reports None; 0.0 is a computed zero.
    """

    name: str
    messages: int
    messages_ready: int
    messages_unacknowledged: int
    consumers: int
    message_rate: float | None  # messages per second; None when not computable this interval
    ack_rate: float | None  # acknowledgments per second; None when not computable this interval


class DatabaseInfo(BaseModel):
    """Model for database information."""

    name: str
    status: str
    connection_count: int
    size: str | None
    error: str | None


class PipelineMetrics(BaseModel):
    """Metrics for a single data pipeline (Discogs or MusicBrainz)."""

    services: list[ServiceStatus]
    queues: list[QueueInfo]
    queues_available: bool = True  # False when the broker scrape failed; `queues` is then empty, not an idle pipeline
    down: bool = False  # True when no service of the pipeline answered: it is down, not absent


class SystemMetrics(BaseModel):
    """Model for system-wide metrics."""

    pipelines: dict[str, PipelineMetrics]
    databases: list[DatabaseInfo]
    timestamp: datetime
    platform: list[PlatformService] = []  # every long-running service outside the pipelines, in display order
    jobs: list[JobStatus] = []  # one-shot jobs: the last run where a durable record is readable


class WebSocketConnections:
    """Own the synchronized set of dashboard WebSocket connections."""

    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self.lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock

    async def add(self, websocket: WebSocket) -> None:
        async with self._get_lock():
            self.connections.add(websocket)
            console_telemetry.record_websocket_connection_delta(1)

    async def discard(self, websocket: WebSocket) -> None:
        async with self._get_lock():
            if websocket in self.connections:
                self.connections.discard(websocket)
                console_telemetry.record_websocket_connection_delta(-1)

    async def close_all(self) -> None:
        async with self._get_lock():
            targets = list(self.connections)
            self.connections.clear()
            console_telemetry.record_websocket_connection_delta(-len(targets))
        for websocket in targets:
            with contextlib.suppress(Exception):
                await websocket.close()

    async def broadcast(self, message: str) -> None:
        if not self.connections:
            return

        async with self._get_lock():
            targets = list(self.connections)

        async def send(websocket: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(websocket.send_text(message), timeout=_WS_SEND_TIMEOUT_SECONDS)
            except Exception:
                return websocket
            return None

        results = await asyncio.gather(*(send(websocket) for websocket in targets))
        disconnected = {websocket for websocket in results if websocket is not None}
        if not disconnected:
            return

        async with self._get_lock():
            self.connections -= disconnected
            console_telemetry.record_websocket_connection_delta(-len(disconnected))

        for websocket in disconnected:
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)


# Queue prefixes are ALWAYS env-derived by the generated catalog contract, exactly like
# the extractor and all four consumers.
# Hardcoding the defaults here made the dashboard the only component that ignored a
# prefix override: the queue-name filter matched nothing, and get_queue_info logs
# nothing on a zero-match 200, so both pipeline panes silently showed an empty,
# healthy-looking pipeline while queues backed up (retained queue-prefix regression).
PIPELINE_CONFIGS: dict[str, dict] = {
    "discogs": {
        "label": "Discogs",
        "producer": "extractor-discogs",
        "consumers": [{"name": "graphinator", "datastore": "neo4j"}, {"name": "tableinator", "datastore": "postgresql"}],
        "services": [
            ("extractor-discogs", "http://extractor-discogs:8000/health"),
            ("graphinator", "http://graphinator:8001/health"),
            ("tableinator", "http://tableinator:8002/health"),
        ],
        "queue_prefix": DISCOGS_EXCHANGE_PREFIX,
        "entity_types": ["masters", "releases", "artists", "labels"],
    },
    "musicbrainz": {
        "label": "MusicBrainz",
        "producer": "extractor-musicbrainz",
        "consumers": [{"name": "brainzgraphinator", "datastore": "neo4j"}, {"name": "brainztableinator", "datastore": "postgresql"}],
        "services": [
            ("extractor-musicbrainz", "http://extractor-musicbrainz:8000/health"),
            ("brainzgraphinator", "http://brainzgraphinator:8011/health"),
            ("brainztableinator", "http://brainztableinator:8010/health"),
        ],
        "queue_prefix": MUSICBRAINZ_EXCHANGE_PREFIX,
        "entity_types": ["release-groups", "releases", "artists", "labels"],
    },
}


class DashboardApp:
    """Main dashboard application."""

    def __init__(self) -> None:
        """Initialize the dashboard application."""
        self.config = get_config()
        self.websockets = WebSocketConnections()
        self.latest_metrics: SystemMetrics | None = None
        self._rate_trackers: dict[str, RateTracker] = {}
        self.events = EventDetector(
            dead_letter_queues=frozenset(
                queue for pipeline in build_topology(PIPELINE_CONFIGS)["pipelines"] for queue in pipeline["dead_letter_queues"]
            )
        )
        self.neo4j_driver: AsyncResilientNeo4jDriver | None = None
        self.postgres_conn: AsyncResilientPostgreSQL | None = None
        self.update_task: asyncio.Task | None = None
        self._db_info_lock: asyncio.Lock | None = None
        self._job_cache: tuple[float, list[JobStatus]] | None = None

    @property
    def websocket_connections(self) -> set[WebSocket]:
        """Compatibility view of the connection set owned by ``websockets``."""
        return self.websockets.connections

    @websocket_connections.setter
    def websocket_connections(self, connections: set[WebSocket]) -> None:
        self.websockets.connections = connections

    @property
    def _ws_lock(self) -> asyncio.Lock | None:
        return self.websockets.lock

    @_ws_lock.setter
    def _ws_lock(self, lock: asyncio.Lock | None) -> None:
        self.websockets.lock = lock

    async def startup(self) -> None:
        """Initialize connections on startup."""
        try:
            self.neo4j_driver = AsyncResilientNeo4jDriver(
                uri=self.config.neo4j_host,
                auth=(self.config.neo4j_username, self.config.neo4j_password),
                max_retries=5,
                **neo4j_security_kwargs(),
            )
            logger.info("🔗 Connected to Neo4j with resilient driver")

            console_telemetry.register_neo4j_gauges(self.neo4j_driver)

            host, port = parse_postgres_host_port(self.config.postgres_host)

            self.postgres_conn = AsyncResilientPostgreSQL(
                connection_params={
                    "host": host,
                    "port": port,
                    "dbname": self.config.postgres_database,
                    "user": self.config.postgres_username,
                    "password": self.config.postgres_password,
                },
                max_retries=5,
            )
            logger.info("🐘 Connected to PostgreSQL with resilient connection")

            self.update_task = asyncio.create_task(self.collect_metrics_loop())
            logger.info("📊 Started metrics collection")

        except Exception as e:
            logger.error(f"❌ Startup error: {e}")
            raise

    async def shutdown(self) -> None:
        """Clean up connections on shutdown."""
        try:
            if self.update_task:
                self.update_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.update_task

            if self.neo4j_driver:
                console_telemetry.reset_neo4j_gauges()
                await self.neo4j_driver.close()
            if self.postgres_conn:
                await self.postgres_conn.close()

            await self.websockets.close_all()

            logger.info("✅ Shutdown complete")

        except Exception as e:
            logger.error(f"❌ Shutdown error: {e}")

    async def collect_metrics_loop(self) -> None:
        """Continuously collect metrics in the background."""
        cycle_count = 0
        while True:
            loop_start = time.perf_counter()
            try:
                metrics = await self.collect_all_metrics()
                self.latest_metrics = metrics
                cycle_count += 1

                # Broadcast to all connected websockets
                await self.broadcast_metrics(metrics)
                await self.broadcast_events(self.events.observe(metrics))

                # Log a summary every 50 cycles (~100s) instead of every request
                if cycle_count % 50 == 0:
                    pipeline_names = list(metrics.pipelines.keys()) if metrics.pipelines else []
                    ws_count = len(self.websocket_connections)
                    logger.info(
                        "📊 Metrics collection summary: cycles=%d, pipelines=%s, websocket_clients=%d",
                        cycle_count,
                        pipeline_names,
                        ws_count,
                    )

                console_telemetry.record_poll_duration(
                    console_telemetry.POLL_TARGET_LOOP, console_telemetry.POLL_OUTCOME_SUCCESS, time.perf_counter() - loop_start
                )

                await asyncio.sleep(2)  # Update every 2 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                console_telemetry.record_poll_duration(
                    console_telemetry.POLL_TARGET_LOOP, console_telemetry.POLL_OUTCOME_FAILURE, time.perf_counter() - loop_start
                )
                logger.error(f"❌ Error collecting metrics: {e}")
                await asyncio.sleep(5)

    async def collect_all_metrics(self) -> SystemMetrics:
        """Collect all system metrics: the pipelines, the platform services, the datastores, and the jobs.

        Every source is read at once. A pipeline none of whose services answered is still reported,
        marked ``down``: it is down, not absent.
        """
        names = list(PIPELINE_CONFIGS)
        pipeline_readings, platform, databases, jobs = await asyncio.gather(
            asyncio.gather(*(self._collect_pipeline(PIPELINE_CONFIGS[name]) for name in names)),
            self.get_platform_health(),
            self.get_database_info(),
            self.get_job_status(),
        )
        return SystemMetrics(
            pipelines=dict(zip(names, pipeline_readings, strict=True)),
            databases=databases,
            timestamp=datetime.now(UTC),
            platform=[*self._platform_with_datastores(platform, databases)],
            jobs=jobs,
        )

    async def _collect_pipeline(self, config: dict) -> PipelineMetrics:
        services, queues = await asyncio.gather(self.get_service_statuses(config["services"]), self.get_queue_info(config["queue_prefix"]))
        return PipelineMetrics(
            services=services,
            queues=queues or [],
            queues_available=queues is not None,
            down=all(service.status == "unknown" for service in services),
        )

    @staticmethod
    def _platform_with_datastores(probed: list[PlatformService], databases: list[DatabaseInfo]) -> list[PlatformService]:
        """The probed services, with the datastores the console is connected to placed first in the Data group."""
        stores = datastore_services(databases)
        first_data = next((index for index, service in enumerate(probed) if service.group == "data"), len(probed))
        return [*probed[:first_data], *stores, *probed[first_data:]]

    async def get_platform_health(self) -> list[PlatformService]:
        """Check every long-running platform service at once."""
        return await poll_platform(build_probes(self.config.rabbitmq_prometheus_url))

    async def get_job_status(self) -> list[JobStatus]:
        """One-shot jobs: the last embeddings load from its durable record, the rest as not monitored.

        Reused for ``JOB_CACHE_SECONDS``: the embeddings run is monthly.
        """
        now = time.monotonic()
        if self._job_cache is not None and now - self._job_cache[0] < JOB_CACHE_SECONDS:
            return self._job_cache[1]
        jobs = [*unmonitored_jobs(), await self._embeddings_job()]
        self._job_cache = (now, jobs)
        return jobs

    async def _embeddings_job(self) -> JobStatus:
        if self._db_info_lock is None:
            self._db_info_lock = asyncio.Lock()
        try:
            if self.postgres_conn is None:
                return embeddings_unreadable()
            async with self._db_info_lock:
                conn = await self.postgres_conn.get_connection()
                async with conn.cursor() as cur:
                    await cur.execute(LAST_EMBEDDINGS_LOAD_SQL)
                    row = await cur.fetchone()
            return embeddings_job(tuple(row) if row else None)
        except Exception as error:
            # No table, no grant for the console's role, or no database: there is no record to read.
            logger.info("Embeddings load record not readable: %s", error)
            return embeddings_unreadable()

    @staticmethod
    async def _poll_service(client: httpx.AsyncClient, name: str, url: str) -> ServiceStatus:
        with console_telemetry.poll_span(name) as poll:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    poll.failed()
                    return ServiceStatus(
                        name=name,
                        status="unhealthy",
                        last_seen=datetime.now(UTC),
                        current_task=None,
                        progress=None,
                        error=f"HTTP {response.status_code}",
                    )

                data = response.json()
                return ServiceStatus(
                    name=name,
                    status=data.get("status", "healthy"),
                    last_seen=datetime.now(UTC),
                    current_task=data.get("current_task"),
                    progress=data.get("progress"),
                    error=None,
                    extraction_progress=data.get("extraction_progress"),
                    last_extraction_time=data.get("last_extraction_time"),
                )
            except Exception as error:
                poll.failed(error)
                return ServiceStatus(name=name, status="unknown", last_seen=None, current_task=None, progress=None, error=str(error))

    async def get_service_statuses(self, service_configs: list[tuple[str, str]]) -> list[ServiceStatus]:
        """Poll the services at once through one shared client; the readings keep configuration order."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            return list(await asyncio.gather(*(self._poll_service(client, name, url) for name, url in service_configs)))

    async def get_queue_info(self, prefix: str) -> list[QueueInfo] | None:
        """Read live queue metrics for queues under ``prefix`` from the RabbitMQ Prometheus endpoint.

        Returns None when the scrape fails, so callers report the data as unavailable instead of
        presenting an empty, healthy-looking pipeline.
        """
        with console_telemetry.poll_span(console_telemetry.POLL_TARGET_RABBITMQ) as poll:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{self.config.rabbitmq_prometheus_url}/metrics/detailed", params=scrape_params(prefix))
                if response.status_code != 200:
                    poll.failed()
                    logger.warning(f"⚠️ RabbitMQ Prometheus endpoint returned status {response.status_code}. Queue metrics unavailable.")
                    return None
                samples = parse_queue_samples(response.text)
            except httpx.ConnectError as e:
                poll.failed(e)
                logger.warning("⚠️ RabbitMQ Prometheus endpoint unreachable. Queue metrics unavailable.")
                return None
            except Exception as e:
                poll.failed(e)
                logger.warning(f"⚠️ Error reading RabbitMQ queue metrics: {e}. Queue metrics unavailable.")
                return None

        rates = self._rate_trackers.setdefault(prefix, RateTracker()).update(samples, time.monotonic())
        return [
            QueueInfo(
                name=queue,
                messages=sample.messages,
                messages_ready=sample.messages_ready,
                messages_unacknowledged=sample.messages_unacknowledged,
                consumers=sample.consumers,
                message_rate=rates[(vhost, queue)][0],
                ack_rate=rates[(vhost, queue)][1],
            )
            for (vhost, queue), sample in sorted(samples.items(), key=lambda item: item[0][1])
        ]

    async def get_database_info(self) -> list[DatabaseInfo]:
        """Get database information."""
        if self._db_info_lock is None:
            self._db_info_lock = asyncio.Lock()
        async with self._db_info_lock:
            return await self._get_database_info_locked()

    async def _get_database_info_locked(self) -> list[DatabaseInfo]:
        """Get database information (must be called under _db_info_lock)."""
        databases = []

        # Check PostgreSQL
        conn = None
        with console_telemetry.poll_span(console_telemetry.POLL_TARGET_POSTGRES) as poll:
            try:
                if self.postgres_conn is None:
                    raise RuntimeError("PostgreSQL resilient connection not initialized")
                # The resilient connection manages the single connection internally; it is
                # never closed here. Autocommit is always True on pool connections, so
                # rollback() is a no-op — omitted per the autocommit contract.
                conn = await self.postgres_conn.get_connection()
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = %s",
                        (self.config.postgres_database,),
                    )
                    result = await cur.fetchone()
                    connection_count = result[0] if result else 0

                    await cur.execute(
                        "SELECT pg_database_size(%s)",
                        (self.config.postgres_database,),
                    )
                    result = await cur.fetchone()
                    raw_bytes = result[0] if result else 0
                    if raw_bytes >= 1024**3:
                        gb = raw_bytes / 1024**3
                        db_size = f"{gb:,.2f} GB"
                    else:
                        mb = raw_bytes / 1024**2
                        db_size = f"{mb:,.0f} MB"

                databases.append(
                    DatabaseInfo(
                        name="PostgreSQL",
                        status="healthy",
                        connection_count=connection_count,
                        size=db_size,
                        error=None,
                    )
                )
            except Exception as e:
                poll.failed(e)
                databases.append(
                    DatabaseInfo(
                        name="PostgreSQL",
                        status="unhealthy",
                        connection_count=0,
                        size=None,
                        error=str(e),
                    )
                )

        # Check Neo4j
        if self.neo4j_driver:
            with console_telemetry.poll_span(console_telemetry.POLL_TARGET_NEO4J) as poll:
                try:
                    async with self.neo4j_driver.session() as session:
                        result = await session.run("CALL dbms.components() YIELD name, versions")
                        await result.single()

                        # Get database size — fall back to an "unknown" marker if APOC
                        # is not installed. node_count/rel_count stay None (never a
                        # fabricated 0) so a fully-populated graph can never render as
                        # "0 nodes, 0 relationships" — a hard count for an unmeasured
                        # quantity is indistinguishable from a genuinely empty graph
                        # and misleads operators into thinking ingestion is broken
                        # (retained APOC fallback regression).
                        node_count: int | None
                        rel_count: int | None
                        try:
                            result = await session.run("CALL apoc.meta.stats() YIELD nodeCount, relCount")
                            stats = await result.single()
                            node_count = stats["nodeCount"] if stats else None
                            rel_count = stats["relCount"] if stats else None
                        except Exception:
                            # APOC not installed — avoid full graph scan which is too
                            # expensive for the 2-second metrics loop on large datasets
                            node_count = None
                            rel_count = None

                        size = (
                            f"{node_count:,} nodes, {rel_count:,} relationships"
                            if node_count is not None and rel_count is not None
                            else "unknown (APOC unavailable)"
                        )

                    databases.append(
                        DatabaseInfo(
                            name="Neo4j",
                            status="healthy",
                            connection_count=1,  # Neo4j doesn't expose this easily
                            size=size,
                            error=None,
                        )
                    )
                except Exception as e:
                    poll.failed(e)
                    databases.append(
                        DatabaseInfo(
                            name="Neo4j",
                            status="unhealthy",
                            connection_count=0,
                            size=None,
                            error=str(e),
                        )
                    )

        return databases

    async def broadcast_metrics(self, metrics: SystemMetrics) -> None:
        """Broadcast metrics to all connected websockets."""
        if not self.websocket_connections:
            return
        message = orjson.dumps(
            {
                "type": "metrics_update",
                "data": metrics.model_dump(mode="json"),
            }
        ).decode()
        await self.websockets.broadcast(message)

    async def broadcast_events(self, events: list[dict[str, Any]]) -> None:
        """Stream newly detected events to every connected websocket."""
        if not events or not self.websocket_connections:
            return
        for event in events:
            await self.websockets.broadcast(orjson.dumps({"type": "event", "data": event}).decode())


# Create the dashboard app instance
dashboard: DashboardApp | None = None


# Create FastAPI app with lifespan
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifecycle."""
    log_file = Path("/logs/dashboard.log")
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        setup_logging("dashboard", log_file=log_file)
    except OSError:
        setup_logging("dashboard")
    setup_telemetry("dashboard")
    instrument_fastapi_app(_app)
    instrument_httpx()
    # The lifespan already runs on the server's event loop, so this is the first place in the
    # process where the loop the console actually serves on can be sampled. It returns None
    # (after one log line) whenever there is nothing to sample into — no endpoint, metrics
    # export off, or no `otel` extra — and shutdown_telemetry() cancels the task on the way
    # out, so nothing here has to be undone in the shutdown half below.
    start_event_loop_monitor()
    logger.info("🚀 Starting GrooveMap operations-console...")

    global dashboard
    dashboard = DashboardApp()
    await dashboard.startup()
    await session_gate.start()
    try:
        yield
    finally:
        await session_gate.aclose()
        await dashboard.shutdown()
        shutdown_telemetry()


class ConsoleApp(FastAPI):
    """FastAPI app that takes the WebSocket ticket out of the query string before anything reads it.

    Middleware, including the OpenTelemetry middleware ``instrument_fastapi_app`` wraps around
    the whole stack, runs inside ``__call__``, so stripping here is the only point every
    request-target reader sits behind.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Strip the ticket, then serve the request."""
        take_ws_ticket(scope)
        await super().__call__(scope, receive, send)


install_ticket_redaction()

# No interactive docs or OpenAPI document: anonymous callers get the login shell and /health
# only. `app.openapi()` still builds the schema, and the AGPL source link stays in the page.
app = ConsoleApp(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    title="GrooveMap Dashboard",
    version="0.1.0",
    license_info={"name": "GNU Affero General Public License v3 only", "identifier": "AGPL-3.0-only"},
    openapi_external_docs={"description": "Corresponding source for this console revision", "url": SOURCE_URL},
    default_response_class=JSONResponse,
    lifespan=lifespan,
)
app.add_exception_handler(SessionRejected, session_rejected_handler)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["http://localhost:3000", "http://localhost:8003"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check() -> JSONResponse:
    """Health check endpoint for Docker and monitoring."""
    return JSONResponse(
        content={
            "status": "healthy",
            "service": "dashboard",
            "timestamp": datetime.now(UTC).isoformat(),
            "uptime": "running",
        }
    )


@app.get("/api/metrics", dependencies=_SESSION_REQUIRED)
async def get_metrics() -> JSONResponse:
    """Get current system metrics."""
    if dashboard and dashboard.latest_metrics:
        return JSONResponse(content=dashboard.latest_metrics.model_dump(mode="json"))
    elif dashboard:
        # Collect metrics on demand if not available
        metrics = await dashboard.collect_all_metrics()
        return JSONResponse(content=metrics.model_dump(mode="json"))
    else:
        return JSONResponse(content={})


@app.get("/api/services", dependencies=_SESSION_REQUIRED)
async def get_services() -> JSONResponse:
    """Get service statuses grouped by pipeline."""
    if not dashboard:
        return JSONResponse(content={})
    result = {}
    for pipeline_name, config in PIPELINE_CONFIGS.items():
        services = await dashboard.get_service_statuses(config["services"])
        if any(s.status != "unknown" for s in services):
            result[pipeline_name] = [s.model_dump(mode="json") for s in services]
    return JSONResponse(content=result)


@app.get("/api/queues", dependencies=_SESSION_REQUIRED)
async def get_queues() -> JSONResponse:
    """Get queue information grouped by pipeline from the live loop's latest snapshot.

    Serving the snapshot (rather than scraping per request) keeps the rate window at the loop's
    two-second poll. A pipeline whose scrape failed is null: unavailable, not an empty pipeline.
    """
    if not dashboard or not dashboard.latest_metrics:
        return JSONResponse(content={})
    result: dict[str, list[dict[str, Any]] | None] = {}
    for pipeline_name, pipeline in dashboard.latest_metrics.pipelines.items():
        if not pipeline.queues_available:
            result[pipeline_name] = None
        elif pipeline.queues:
            result[pipeline_name] = [q.model_dump(mode="json") for q in pipeline.queues]
    return JSONResponse(content=result)


@app.get("/api/queue-prefixes", dependencies=_SESSION_REQUIRED)
async def get_queue_prefixes() -> JSONResponse:
    """Expose the env-derived exchange/queue prefixes to the admin frontend.

    admin.js builds DLQ names it POSTs to the API, which validates them against
    _VALID_DLQ_NAMES (itself env-derived). Its compiled-in literals are only defaults;
    under a DISCOGS_EXCHANGE_PREFIX / MUSICBRAINZ_EXCHANGE_PREFIX override every Purge
    button would 404 without this (retained queue-prefix regression).
    """
    return JSONResponse(content={"discogs": DISCOGS_EXCHANGE_PREFIX, "musicbrainz": MUSICBRAINZ_EXCHANGE_PREFIX})


@app.get("/api/topology", dependencies=_SESSION_REQUIRED)
async def get_topology() -> JSONResponse:
    """Describe the pipelines the Overview lays out: producers, per-entity queues, consumers, DLQs, datastores."""
    return JSONResponse(content=build_topology(PIPELINE_CONFIGS))


@app.get("/api/databases", dependencies=_SESSION_REQUIRED)
async def get_databases() -> JSONResponse:
    """Get database information."""
    if not dashboard:
        return JSONResponse(content=[])
    databases = await dashboard.get_database_info()
    return JSONResponse(content=[d.model_dump(mode="json") for d in databases])


@app.get("/api/session")
async def get_session(session: AdminSession = Depends(require_session)) -> JSONResponse:
    """Report the live admin session; the console shell polls this to notice expiry or revocation."""
    return JSONResponse(
        content={"admin_id": session.admin_id, "email": session.email, "expires_at": session.expires_at.isoformat()},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/ws-ticket")
async def issue_ws_ticket(request: Request) -> JSONResponse:
    """Mint a single-use ticket, valid for 30 seconds, that opens one live WebSocket."""
    session, token = await session_gate.validate(request.headers.get("authorization"))
    ticket, lifetime = session_gate.tickets.issue(session, token)
    return JSONResponse(content={"ticket": ticket, "expires_in": int(lifetime)}, headers={"Cache-Control": "no-store"})


def _ws_close_code(rejected: SessionRejected) -> int:
    """4401 when the session is missing or dead; 4503 when catalog-api could not answer."""
    return WS_CLOSE_UNAVAILABLE if rejected.reason == "unavailable" else WS_CLOSE_UNAUTHENTICATED


async def _discard_websocket(websocket: WebSocket) -> None:
    if dashboard and dashboard._ws_lock:
        async with dashboard._ws_lock:
            if websocket in dashboard.websocket_connections:
                dashboard.websocket_connections.discard(websocket)
                console_telemetry.record_websocket_connection_delta(-1)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time updates, opened with a ticket from ``POST /api/ws-ticket``.

    The ticket is redeemed and its bearer re-validated with catalog-api, bypassing the session
    cache, before anything is sent. A missing, reused, expired, or logged-out ticket is accepted
    only to be closed with 4401: a close before accept reaches the browser as a bare handshake
    failure, which it cannot tell from a network drop. While open, the socket re-validates its
    session against catalog-api (again bypassing the cache) on a fixed 30-second schedule and at
    the session's expiry, and closes with 4401 as soon as catalog-api stops honouring it, or with
    4503 when catalog-api cannot answer, so the browser backs off instead of signing out. The
    console never expects client frames; any that arrive are discarded and cannot postpone a
    re-check.
    """
    grant = session_gate.tickets.redeem(websocket.scope.get(WS_TICKET_SCOPE_KEY))
    await websocket.accept()
    if grant is None:
        await websocket.close(code=WS_CLOSE_UNAUTHENTICATED)
        return
    try:
        session = await session_gate.validate_token(grant.token, fresh=True)
    except SessionRejected as rejected:
        await websocket.close(code=_ws_close_code(rejected))
        return

    if dashboard:
        if dashboard._ws_lock is None:
            dashboard._ws_lock = asyncio.Lock()
        async with dashboard._ws_lock:
            dashboard.websocket_connections.add(websocket)
            console_telemetry.record_websocket_connection_delta(1)

    try:
        # Send initial metrics
        if dashboard and dashboard.latest_metrics:
            await websocket.send_text(
                orjson.dumps(
                    {
                        "type": "metrics_update",
                        "data": dashboard.latest_metrics.model_dump(mode="json"),
                    }
                ).decode()
            )

        # The recent events follow, so a reconnecting client does not start with an empty log.
        if dashboard:
            await websocket.send_text(orjson.dumps({"type": "event_backlog", "data": dashboard.events.backlog()}).decode())

        # Keep the connection alive until the client leaves or the session ends. The re-check
        # deadline is monotonic and only moves after a re-check, so client frames cannot hold
        # it off; it is tested before every receive, so a client that always has a frame
        # queued cannot starve it either.
        next_check = time.monotonic() + _WS_SESSION_RECHECK_SECONDS
        while True:
            if time.monotonic() >= next_check or session.seconds_left() <= 0:
                try:
                    session = await session_gate.validate_token(grant.token, fresh=True)
                except SessionRejected as rejected:
                    await _discard_websocket(websocket)
                    await websocket.close(code=_ws_close_code(rejected))
                    return
                next_check = time.monotonic() + _WS_SESSION_RECHECK_SECONDS
            wait = max(min(next_check - time.monotonic(), session.seconds_left()), 0.0)
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=wait)
            except TimeoutError:
                continue
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            # Any other frame is not part of the console's protocol and is discarded.

    except WebSocketDisconnect:
        await _discard_websocket(websocket)
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
        await _discard_websocket(websocket)


# Admin proxy routes (must be before StaticFiles catch-all)
# Note: reads env vars at import time; this is acceptable because
# Docker containers set env vars before module import.
_admin_api_host = os.getenv("API_HOST", "api")
_admin_api_port = int(os.getenv("API_PORT", "8004"))
configure_admin_proxy(_admin_api_host, _admin_api_port)
session_gate.configure(_admin_api_host, _admin_api_port, cache_seconds=cache_seconds_from_env(os.getenv("SESSION_CACHE_SECONDS")))
# Login and logout are mounted without the console's gate: catalog-api validates both itself, and
# logout must reach it even when the console cannot confirm the session. Every other admin route
# also needs a live session here, so an anonymous request is refused before it reaches catalog-api.
app.include_router(admin_public_router)
app.include_router(admin_router, dependencies=_SESSION_REQUIRED)
app.include_router(history_router, dependencies=_SESSION_REQUIRED)
configure_progress(build_topology(PIPELINE_CONFIGS)["pipelines"])
app.include_router(progress_router, dependencies=_SESSION_REQUIRED)
app.include_router(grafana_alerts_router, dependencies=_SESSION_REQUIRED)
app.include_router(deep_links_router, dependencies=_SESSION_REQUIRED)


@app.get("/admin")
@app.get("/admin.html")
async def redirect_admin() -> RedirectResponse:
    """The admin tabs live in the console shell at /; keep old admin links working."""
    return RedirectResponse("/", status_code=307)


# Mount static files for the UI
static_dir = Path(__file__).parent / "static"


@app.get("/")
@app.get("/index.html")
async def serve_index() -> HTMLResponse:
    """Serve the console shell; it shows only the login form until a session is confirmed."""
    index_path = static_dir / "index.html"
    try:
        content = index_path.read_text().replace("__GROOVEMAP_SOURCE_URL__", SOURCE_URL)
    except FileNotFoundError:
        return HTMLResponse("<h1>Console page not found</h1>", status_code=404)
    return HTMLResponse(content)


app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


def main() -> None:  # pragma: no cover
    """Entry point for the GrooveMap operations-console service."""
    setup_logging("dashboard", log_file=Path("/logs/dashboard.log"))
    print(STARTUP_BANNER)
    uvicorn.run(
        "dashboard.dashboard:app",
        host="0.0.0.0",  # noqa: S104  # nosec B104
        port=8003,
        reload=False,
        log_level=os.getenv("LOG_LEVEL", "INFO").lower(),
    )


if __name__ == "__main__":
    main()
