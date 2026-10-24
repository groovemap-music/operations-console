"""Console test application: the production routes over a fixed metrics source.

``create_test_app`` serves the console's real route table, including the session gate on every
data route, the ticketed WebSocket, the admin proxy, and the static shell, over a
``DashboardApp`` whose collectors return fixed readings instead of polling services, RabbitMQ,
Neo4j, and PostgreSQL. Session validation goes wherever ``dashboard.session_auth.gate`` points:
the in-process fake catalog-api in unit tests, or the standalone fake server the browser suite
starts and names through ``API_HOST`` / ``API_PORT``.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable  # noqa: TC003  # FastAPI resolves lifespan annotations at runtime.
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi import FastAPI  # noqa: TC002  # FastAPI resolves lifespan annotations at runtime.
from fastapi.responses import JSONResponse

from dashboard import dashboard as console
from dashboard.catalog_contract import DISCOGS_EXCHANGE_PREFIX, MUSICBRAINZ_EXCHANGE_PREFIX
from dashboard.config import DashboardConfig
from dashboard.dashboard import DashboardApp
from dashboard.session_auth import SessionRejected, session_rejected_handler


_CONFIG = DashboardConfig(
    neo4j_host="neo4j://localhost:7687",
    neo4j_username="test",
    neo4j_password="test",
    postgres_host="localhost:5432",
    postgres_username="test",
    postgres_password="test",
    postgres_database="test",
)

_MUSICBRAINZ_SERVICES = {"extractor-musicbrainz", "brainzgraphinator", "brainztableinator"}

# Readings are built on each call from `console.*`, never bound at import: a test that reloads
# dashboard.dashboard replaces the model classes the production aggregation validates against.
_QUEUE_READINGS: dict[str, list[tuple[str, int, int, int, float, float]]] = {
    DISCOGS_EXCHANGE_PREFIX: [
        (f"{DISCOGS_EXCHANGE_PREFIX}-graphinator-artists", 10, 5, 2, 0.5, 0.3),
        (f"{DISCOGS_EXCHANGE_PREFIX}-tableinator-artists", 8, 3, 1, 0.4, 0.2),
    ],
    MUSICBRAINZ_EXCHANGE_PREFIX: [
        (f"{MUSICBRAINZ_EXCHANGE_PREFIX}-brainzgraphinator-artists", 5, 2, 1, 0.3, 0.2),
    ],
}


class MockDashboardApp(DashboardApp):
    """The real dashboard aggregation and broadcast loop over fixed collector readings."""

    def __init__(self, include_musicbrainz: bool = False) -> None:
        """Initialise without reading datastore configuration from the environment."""
        with patch("dashboard.dashboard.get_config", return_value=_CONFIG):
            super().__init__()
        self.include_musicbrainz = include_musicbrainz

    async def startup(self) -> None:
        """Run the real collection loop; no datastore clients are opened."""
        self.update_task = asyncio.create_task(self.collect_metrics_loop())

    async def shutdown(self) -> None:
        """Stop the loop and close any live sockets."""
        if self.update_task:
            self.update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.update_task
        await self.websockets.close_all()

    async def get_service_statuses(self, service_configs: list[tuple[str, str]]) -> list[console.ServiceStatus]:
        """Every Discogs service is healthy; MusicBrainz services only when deployed."""
        statuses = []
        for name, _url in service_configs:
            deployed = self.include_musicbrainz or name not in _MUSICBRAINZ_SERVICES
            statuses.append(
                console.ServiceStatus(
                    name=name,
                    status="healthy" if deployed else "unknown",
                    last_seen=datetime(2024, 1, 1, tzinfo=UTC) if deployed else None,
                    current_task=None,
                    progress=None,
                    error=None if deployed else "not deployed",
                )
            )
        return statuses

    async def get_queue_info(self, prefix: str) -> list[console.QueueInfo] | None:
        """Fixed queue readings for the pipeline behind ``prefix``."""
        return [
            console.QueueInfo(
                name=name, messages=messages, messages_ready=ready, messages_unacknowledged=unacked, consumers=1, message_rate=rate, ack_rate=ack
            )
            for name, messages, ready, unacked, rate, ack in _QUEUE_READINGS.get(prefix, [])
        ]

    async def get_database_info(self) -> list[console.DatabaseInfo]:
        """Fixed, healthy datastore readings."""
        return [
            console.DatabaseInfo(name="PostgreSQL", status="healthy", connection_count=5, size="100.5 MB", error=None),
            console.DatabaseInfo(name="Neo4j", status="healthy", connection_count=1, size="1,000 nodes, 5,000 relationships", error=None),
        ]


def _lifespan(include_musicbrainz: bool) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        previous = console.dashboard
        console.dashboard = MockDashboardApp(include_musicbrainz=include_musicbrainz)
        await console.dashboard.startup()
        await console.session_gate.start()
        try:
            yield
        finally:
            await console.session_gate.aclose()
            await console.dashboard.shutdown()
            console.dashboard = previous

    return lifespan


def create_test_app(include_musicbrainz: bool = False) -> FastAPI:
    """Create the console app with the production routes and a fixed metrics source."""
    app = console.ConsoleApp(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        title="GrooveMap Dashboard",
        version="0.1.0",
        default_response_class=JSONResponse,
        lifespan=_lifespan(include_musicbrainz),
    )
    app.add_exception_handler(SessionRejected, session_rejected_handler)
    app.router.routes.extend(console.app.router.routes)
    return app


def create_test_app_with_musicbrainz() -> FastAPI:
    """Create the console test app with the MusicBrainz pipeline deployed."""
    return create_test_app(include_musicbrainz=True)
