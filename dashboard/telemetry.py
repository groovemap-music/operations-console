"""OpenTelemetry domain instruments for operations-console.

Bootstrap (``setup_telemetry`` / ``shutdown_telemetry`` / HTTP instrumentation) lives in
``dashboard.dashboard``, called once from the FastAPI lifespan. This module owns the two
console-specific instruments and is a thin wrapper the rest of the package calls into, mirroring
``groovemap-runtime``'s own ``common.runtime_metrics`` module.

Instruments are built lazily from ``get_meter("groovemap.console")`` on first use and cached
until the installed provider changes (tracked via ``provider_generation()``), so a process that
never calls ``setup_telemetry`` pays only for one no-op instrument per metric. Every recording
helper swallows its own errors: telemetry must never turn a working poll into a failure.

It also owns the one domain span this service opens, ``console.poll {target}``, through the
:func:`poll_span` context manager. Every other span the console produces comes from the
library: the FastAPI and httpx instrumentors supply the HTTP server and client spans, and the
resilient Neo4j, PostgreSQL, and RabbitMQ wrappers supply the database and messaging spans.

Metric names, units, attribute keys, and span names follow the GrooveMap OpenTelemetry
conventions. All attribute values are closed, low-cardinality sets — never ids, hostnames, or
free text, and an error is reported as status ERROR with ``error.type`` and nothing else.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from threading import RLock
from typing import TYPE_CHECKING, Any

from common import get_meter, get_tracer
from common.telemetry import provider_generation
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode


if TYPE_CHECKING:
    from collections.abc import Iterator


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


class PollOutcome:
    """The outcome a `poll_span` block reports back to the span it runs in.

    A poll's own error handling almost never lets an exception escape — `get_service_statuses`
    turns a dead service into an "unknown" row, `get_queue_info` logs and returns an empty
    list — so the block has to be able to say "this attempt failed" without raising. Callers
    flip it with `failed()`; anything that does raise is caught by `poll_span` itself.

    `failed()` takes the exception when there was one. A poll that died on a refused connection
    is a real error and gets span status ERROR with that exception's class name as `error.type`,
    exactly as if it had propagated. A poll that merely got an answer it did not want — an HTTP
    404 from a service that is up, a 401 from the RabbitMQ management API — is a result the
    console handled, so it is reported through the `outcome` attribute and leaves the span
    status unset. Neither form ever attaches the exception message, its traceback, or a span
    event: `error.type` is the whole error report.
    """

    def __init__(self) -> None:
        """Start out successful; only an explicit `failed()` or a raised exception changes it."""
        self.value = POLL_OUTCOME_SUCCESS
        self.error_type: str | None = None

    def failed(self, error: BaseException | None = None) -> None:
        """Mark this poll a failure, optionally naming the exception that caused it."""
        self.value = POLL_OUTCOME_FAILURE
        if error is not None:
            self.error_type = type(error).__name__


@contextmanager
def poll_span(target: str) -> Iterator[PollOutcome]:
    """Run one poll of `target` inside a `console.poll {target}` root span, timing it.

    The span is a trace ROOT by construction (`context=Context()` starts from an empty context
    rather than whatever span happens to be current). Two callers reach these polls: the
    background `collect_metrics_loop`, which has no ambient span, and the `/api/*` handlers,
    which run inside the FastAPI instrumentor's SERVER span. Without the empty context the same
    poll would be a root in one case and a nested child in the other, and the GrooveMap span
    conventions name `console.poll {target}` a domain root span. The outbound httpx request,
    and any Neo4j or PostgreSQL work the wrappers instrument, become children of it.

    `target` comes from the closed poll-target set — a configured service key, or one of
    rabbitmq / neo4j / postgres — never from a URL or a response, so the span name stays
    low-cardinality. The reserved "loop" target is deliberately not given a span: it measures
    the whole collection cycle, so a span for it would make every real poll span its child and
    none of them a root.

    On the way out the block's outcome is recorded twice, once as the span's `outcome`
    attribute and once as the `groovemap.console.poll.duration` histogram point, so the two
    signals can never disagree. An exception — one that escapes the block, or one the block
    caught and handed to `PollOutcome.failed` — sets span status ERROR with `error.type` only:
    no message, no stack trace, no span event carrying a payload, which is why the span opts
    out of the SDK's own exception recording.
    """
    outcome = PollOutcome()
    started = time.perf_counter()
    span_name = f"console.poll {target}"
    # Fetched per call rather than cached at import: tests install an in-memory tracer provider
    # directly into `common.telemetry`, and a poll every two seconds does not need the saving.
    tracer = get_tracer(INSTRUMENTATION_SCOPE)
    with tracer.start_as_current_span(span_name, context=Context(), record_exception=False, set_status_on_exception=False) as span:
        try:
            yield outcome
        except Exception as exc:
            outcome.failed(exc)
            raise
        finally:
            span.set_attribute("target", target)
            span.set_attribute("outcome", outcome.value)
            if outcome.error_type is not None:
                span.set_attribute("error.type", outcome.error_type)
                span.set_status(Status(StatusCode.ERROR))
            record_poll_duration(target, outcome.value, time.perf_counter() - started)
