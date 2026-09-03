"""OpenTelemetry domain instruments for operations-console.

Bootstrap (``setup_telemetry`` / ``shutdown_telemetry`` / HTTP instrumentation) lives in
``dashboard.dashboard``, called once from the FastAPI lifespan. This module owns the two
console-specific instruments and is a thin wrapper the rest of the package calls into, mirroring
``groovemap-runtime``'s own ``common.runtime_metrics`` module.

Instruments are built lazily from ``get_meter("groovemap.console")`` on first use and cached
until the installed provider changes (tracked via ``provider_generation()``), so a process that
never calls ``setup_telemetry`` pays only for one no-op instrument per metric. Every recording
helper swallows its own errors: telemetry must never turn a working poll into a failure.

Metric names, units, and attribute keys follow the GrooveMap OpenTelemetry conventions. All
attribute values are closed, low-cardinality sets — never ids, hostnames, or free text.
"""

import logging
from threading import RLock
from typing import Any

from common import get_meter
from common.telemetry import provider_generation


logger = logging.getLogger(__name__)

INSTRUMENTATION_SCOPE = "groovemap.console"

WEBSOCKET_CONNECTIONS = "groovemap.console.websocket.connections"
POLL_DURATION = "groovemap.console.poll.duration"

# The closed set of poll.duration `target` attribute values: one per polled service key plus
# the three infrastructure targets. `collect_metrics_loop`'s own end-to-end duration uses the
# reserved "loop" target so a slow overall cycle is visible even when every individual poll
# looks fine.
POLL_TARGET_RABBITMQ = "rabbitmq"
POLL_TARGET_NEO4J = "neo4j"
POLL_TARGET_POSTGRES = "postgres"
POLL_TARGET_LOOP = "loop"

POLL_OUTCOME_SUCCESS = "success"
POLL_OUTCOME_FAILURE = "failure"

_lock = RLock()
_instruments: dict[str, Any] = {}
_instrument_generation = -1


def _build_instruments() -> dict[str, Any]:
    """Create one instrument per console metric from the current provider."""
    meter = get_meter(INSTRUMENTATION_SCOPE)
    return {
        WEBSOCKET_CONNECTIONS: meter.create_up_down_counter(
            WEBSOCKET_CONNECTIONS,
            description="Number of active WebSocket connections.",
        ),
        POLL_DURATION: meter.create_histogram(
            POLL_DURATION,
            unit="s",
            description="Duration of a status poll against one service, RabbitMQ, Neo4j, or PostgreSQL.",
        ),
    }


def _instrument(name: str) -> Any:
    """Return one cached instrument, rebuilding the cache when the provider changed."""
    global _instrument_generation

    generation = provider_generation()
    with _lock:
        if _instrument_generation != generation or not _instruments:
            _instruments.clear()
            _instruments.update(_build_instruments())
            _instrument_generation = generation
        return _instruments[name]


def reset_instruments() -> None:
    """Drop the instrument cache. Test seam; production relies on the generation check."""
    global _instrument_generation

    with _lock:
        _instruments.clear()
        _instrument_generation = -1


def record_websocket_connection_delta(delta: int) -> None:
    """Adjust the active WebSocket connection count by `delta` (+1 connect, -1 disconnect)."""
    try:
        _instrument(WEBSOCKET_CONNECTIONS).add(delta)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not record %s", WEBSOCKET_CONNECTIONS, exc_info=True)


def record_poll_duration(target: str, outcome: str, duration_s: float) -> None:
    """Record one poll's duration against `target` (a service key, rabbitmq, neo4j, or postgres)."""
    try:
        _instrument(POLL_DURATION).record(duration_s, {"target": target, "outcome": outcome})
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not record %s", POLL_DURATION, exc_info=True)
