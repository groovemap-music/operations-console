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

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Any

from common import get_meter, get_tracer
from common.telemetry import provider_generation
from opentelemetry.context import Context
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import Status, StatusCode


if TYPE_CHECKING:
    from asyncio import AbstractEventLoop
    from collections.abc import Iterable, Iterator


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


# ---------------------------------------------------------------------------
# Neo4j observable gauges
#
# Neo4j Community exposes no Prometheus endpoint, so the console — which already holds a driver
# and already polls every target — is the program's designated observer for the graph store.
# ---------------------------------------------------------------------------

NEO4J_UP = "groovemap.neo4j.up"
NEO4J_NODES = "groovemap.neo4j.nodes"
NEO4J_RELATIONSHIPS = "groovemap.neo4j.relationships"
NEO4J_TRANSACTIONS_ACTIVE = "groovemap.neo4j.transactions.active"
NEO4J_STORE_SIZE_BYTES = "groovemap.neo4j.store.size.bytes"

# The closed node-label and relationship-type sets the GrooveMap graph schema defines.
# database-schema owns the schema itself; these are the label and type names its constraints
# and the deployment architecture inventory enumerate, pinned here so a schema change that
# adds or drops one fails this repository's tests instead of silently changing the metric's
# attribute set under the dashboards and alert rules that read it.
NEO4J_NODE_LABELS: tuple[str, ...] = (
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

NEO4J_RELATIONSHIP_TYPES: tuple[str, ...] = (
    # Discogs-sourced and taxonomy edges.
    "ALIAS_OF",
    "BY",
    "COLLECTED",
    "CREDITED_ON",
    "DERIVED_FROM",
    "IN_FAMILY",
    "IS",
    "ISSUED_ON",
    "MEMBER_OF",
    "ON",
    "PART_OF",
    "SAME_AS",
    "SUBLABEL_OF",
    "WANTS",
    # MusicBrainz-sourced edges, all carrying source: 'musicbrainz'.
    "COLLABORATED_WITH",
    "FOUNDED",
    "RENAMED_TO",
    "SUBGROUP_OF",
    "SUPPORTED",
    "TAUGHT",
    "TRIBUTE_TO",
)

# The "Store sizes" JMX bean's attributes, mapped to the closed `store` attribute values the
# gauge reports. Anything the bean returns that is not listed here is dropped rather than
# passed through, so a Neo4j upgrade cannot widen the attribute set on its own.
NEO4J_STORE_ATTRIBUTES: dict[str, str] = {
    "ArrayStore": "array",
    "CountStore": "count",
    "IndexStore": "index",
    "LabelStore": "label",
    "LogicalLog": "transaction_log",
    "NodeStore": "node",
    "PropertyStore": "property",
    "RelationshipStore": "relationship",
    "SchemaStore": "schema",
    "StringStore": "string",
    "TotalStoreSize": "total",
}


def _count_branch(inner_match: str, name: str) -> str:
    """Return one count-store branch that also carries the closed-set `name` it counted.

    The aggregation has to be ALONE in a RETURN for Neo4j to answer it from the count store.
    Neo4j only plans `NodeCountFromCountStore` / `RelationshipCountFromCountStore` when nothing
    else is projected alongside `count()`; adding the label as a grouping key —
    `MATCH (n:Artist) RETURN 'Artist' AS name, count(n) AS count` — silently demotes the plan to
    `NodeByLabelScan` plus `EagerAggregation`, which reads every node. Profiled on Neo4j 5.26.30
    community over 5,000 Artist nodes and 4,000 BY relationships: 5,001 and 4,001 database hits
    for the grouped form against 1 for the form below. At GrooveMap's scale — hundreds of
    millions of nodes and relationships — that is a full store scan per label per export
    interval, which would blow the two-second query budget and pin `groovemap.neo4j.up` to 0.

    Wrapping the aggregation in a `CALL () { ... }` scoped subquery keeps it alone inside the
    subquery and adds the literal outside it, so the plan is `NodeCountFromCountStore` followed
    by a `Projection`: one database hit per branch, and still one round trip for the group.
    """
    return f"CALL () {{ {inner_match} }}\nRETURN '{name}' AS name, count"


# One UNION ALL round trip per group instead of thirty-one, every branch answered from the
# count store, so the whole query costs O(labels) database hits and never touches a node or a
# relationship. Measured on Neo4j 5.26.30 community: 10 hits for the ten labels, 21 for the
# twenty-one relationship types, with no scan operator in either plan.
NEO4J_NODE_COUNT_QUERY = "\nUNION ALL\n".join(_count_branch(f"MATCH (n:{label}) RETURN count(n) AS count", label) for label in NEO4J_NODE_LABELS)
NEO4J_RELATIONSHIP_COUNT_QUERY = "\nUNION ALL\n".join(
    _count_branch(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS count", rel) for rel in NEO4J_RELATIONSHIP_TYPES
)

NEO4J_TRANSACTION_QUERY = "SHOW TRANSACTIONS YIELD transactionId RETURN count(*) AS count"
NEO4J_STORE_SIZE_QUERY = "CALL dbms.queryJmx('org.neo4j:instance=kernel#0,name=Store sizes')"

# Each query gets its own two-second budget, and the refresh as a whole gets one that covers
# every query plus the hand-off, so the exporter thread has a hard bound even if the driver
# stops honouring cancellation.
NEO4J_QUERY_TIMEOUT_S = 2.0
NEO4J_REFRESH_BUDGET_S = 10.0

_DEFAULT_EXPORT_INTERVAL_MS = 60000.0


@dataclass(frozen=True)
class Neo4jSnapshot:
    """One reading of the graph store, shared by every gauge callback in an export cycle.

    `up` false is the only thing reported when a refresh fails: the other fields stay empty so
    the callbacks observe nothing at all. A zero would be indistinguishable from a genuinely
    empty graph and would drag every dashboard average and every alert threshold down with it,
    which is exactly the failure mode the console's own APOC fallback already guards against.
    """

    up: bool
    nodes: dict[str, int] = field(default_factory=dict)
    relationships: dict[str, int] = field(default_factory=dict)
    transactions_active: int | None = None
    store_sizes: dict[str, int] = field(default_factory=dict)


NEO4J_DOWN = Neo4jSnapshot(up=False)


def _export_interval_s() -> float:
    """Return the configured metric export interval in seconds, defaulting to the SDK's 60."""
    raw = os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "")
    try:
        interval_ms = float(raw)
    except ValueError:
        interval_ms = _DEFAULT_EXPORT_INTERVAL_MS
    if interval_ms <= 0:
        interval_ms = _DEFAULT_EXPORT_INTERVAL_MS
    return interval_ms / 1000.0


def _monotonic() -> float:
    """Return the monotonic clock the snapshot's freshness is measured against.

    Indirected through this function so a test can move the clock forward without patching the
    `time` module itself, which the event loop also reads.
    """
    return time.monotonic()


def _refresh_ttl_s() -> float:
    """Return how long a snapshot stays fresh: half an export interval.

    All five callbacks run back to back inside one collection, milliseconds apart, so half an
    interval is comfortably long enough for the last of them to reuse what the first fetched
    and comfortably short enough that the next collection, a full interval later, always
    refreshes. That is the whole "one refresh per export interval" guarantee.
    """
    return max(1.0, _export_interval_s() / 2.0)


class _Neo4jGaugeSource:
    """Owns the shared snapshot and the hand-off from the exporter thread to the event loop.

    The gauge callbacks run on the metric reader's own thread while the driver they need is
    async and bound to the console's event loop, so every refresh is submitted to that loop
    with `run_coroutine_threadsafe` and waited on with a hard timeout. Nothing here raises: a
    failed, timed-out, or un-submittable refresh becomes `NEO4J_DOWN`, which reports up=0 and
    nothing else.
    """

    def __init__(self, driver: Any, loop: AbstractEventLoop) -> None:
        """Bind the source to one driver and the loop that driver's coroutines must run on."""
        self._driver = driver
        self._loop = loop
        self._guard = RLock()
        self._snapshot: Neo4jSnapshot | None = None
        self._refreshed_at = 0.0

    def snapshot(self) -> Neo4jSnapshot:
        """Return the current reading, refreshing it at most once per export interval."""
        with self._guard:
            now = _monotonic()
            if self._snapshot is not None and now - self._refreshed_at < _refresh_ttl_s():
                return self._snapshot
            self._snapshot = self._refresh()
            self._refreshed_at = now
            return self._snapshot

    def _refresh(self) -> Neo4jSnapshot:
        """Run one read on the console's event loop, bounded, never raising."""
        read = self._read()
        try:
            pending = asyncio.run_coroutine_threadsafe(read, self._loop)
        except Exception:
            # The loop is closed or shutting down — the console is on its way out. Closing the
            # coroutine that was never scheduled keeps a "never awaited" warning out of the
            # exporter thread's log.
            read.close()
            logger.debug("Could not submit the Neo4j gauge refresh", exc_info=True)
            return NEO4J_DOWN
        try:
            return pending.result(timeout=NEO4J_REFRESH_BUDGET_S)
        except Exception:
            pending.cancel()
            logger.debug("Neo4j gauge refresh did not complete", exc_info=True)
            return NEO4J_DOWN

    async def _read(self) -> Neo4jSnapshot:
        """Read every gauge's backing number in one session, or report the store down."""
        try:
            async with self._driver.session() as session:
                nodes = await self._counts(session, NEO4J_NODE_COUNT_QUERY, NEO4J_NODE_LABELS)
                relationships = await self._counts(session, NEO4J_RELATIONSHIP_COUNT_QUERY, NEO4J_RELATIONSHIP_TYPES)
                transactions = await self._transactions(session)
                # The JMX bean is the one optional reading: it is absent on some deployments,
                # and its absence says nothing about whether the store is healthy.
                stores = await self._store_sizes(session)
        except Exception:
            logger.debug("Neo4j is not answering the gauge queries", exc_info=True)
            return NEO4J_DOWN
        return Neo4jSnapshot(up=True, nodes=nodes, relationships=relationships, transactions_active=transactions, store_sizes=stores)

    @staticmethod
    async def _rows(session: Any, query: str) -> list[dict[str, Any]]:
        """Run one query and read its rows, both inside a single two-second budget."""

        async def run() -> list[dict[str, Any]]:
            result = await session.run(query)
            rows: list[dict[str, Any]] = await result.data()
            return rows

        return await asyncio.wait_for(run(), NEO4J_QUERY_TIMEOUT_S)

    async def _counts(self, session: Any, query: str, expected: tuple[str, ...]) -> dict[str, int]:
        """Return {name: count} for the closed set `expected`, ignoring anything else."""
        allowed = set(expected)
        counts: dict[str, int] = {}
        for row in await self._rows(session, query):
            name = row.get("name")
            count = row.get("count")
            if isinstance(name, str) and name in allowed and isinstance(count, int):
                counts[name] = count
        return counts

    async def _transactions(self, session: Any) -> int | None:
        """Return the number of transactions Neo4j currently reports, including this one."""
        for row in await self._rows(session, NEO4J_TRANSACTION_QUERY):
            count = row.get("count")
            if isinstance(count, int):
                return count
        return None

    async def _store_sizes(self, session: Any) -> dict[str, int]:
        """Return {store: bytes} from the JMX bean, or nothing at all when it does not answer.

        Every other reading has already succeeded by the time this runs, so a missing procedure
        — it is not installed on every Neo4j edition or configuration — must not turn a healthy
        store into up=0. The sizes are simply omitted.
        """
        try:
            rows = await self._rows(session, NEO4J_STORE_SIZE_QUERY)
        except Exception:
            logger.debug("Neo4j did not answer the Store sizes JMX query", exc_info=True)
            return {}
        sizes: dict[str, int] = {}
        for row in rows:
            attributes = row.get("attributes")
            if not isinstance(attributes, dict):
                continue
            for jmx_name, store in NEO4J_STORE_ATTRIBUTES.items():
                entry = attributes.get(jmx_name)
                value = entry.get("value") if isinstance(entry, dict) else entry
                if isinstance(value, int) and not isinstance(value, bool):
                    sizes[store] = value
        return sizes


_neo4j_source: _Neo4jGaugeSource | None = None
_neo4j_gauge_generation = -1


def _neo4j_snapshot() -> Neo4jSnapshot | None:
    """Return the shared snapshot, or None when no driver has been registered."""
    with _lock:
        source = _neo4j_source
    return None if source is None else source.snapshot()


def _observe_up(options: CallbackOptions) -> Iterable[Observation]:  # noqa: ARG001
    """Observe 1 when the last refresh answered, 0 when it did not."""
    try:
        snapshot = _neo4j_snapshot()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not observe %s", NEO4J_UP, exc_info=True)
        return []
    return [] if snapshot is None else [Observation(1 if snapshot.up else 0)]


def _observe_nodes(options: CallbackOptions) -> Iterable[Observation]:  # noqa: ARG001
    """Observe one node count per schema label, or nothing while the store is down."""
    try:
        snapshot = _neo4j_snapshot()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not observe %s", NEO4J_NODES, exc_info=True)
        return []
    if snapshot is None:
        return []
    return [Observation(count, {"label": label}) for label, count in snapshot.nodes.items()]


def _observe_relationships(options: CallbackOptions) -> Iterable[Observation]:  # noqa: ARG001
    """Observe one relationship count per schema type, or nothing while the store is down."""
    try:
        snapshot = _neo4j_snapshot()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not observe %s", NEO4J_RELATIONSHIPS, exc_info=True)
        return []
    if snapshot is None:
        return []
    return [Observation(count, {"type": name}) for name, count in snapshot.relationships.items()]


def _observe_transactions(options: CallbackOptions) -> Iterable[Observation]:  # noqa: ARG001
    """Observe the active transaction count, or nothing while the store is down."""
    try:
        snapshot = _neo4j_snapshot()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not observe %s", NEO4J_TRANSACTIONS_ACTIVE, exc_info=True)
        return []
    if snapshot is None or snapshot.transactions_active is None:
        return []
    return [Observation(snapshot.transactions_active)]


def _observe_store_sizes(options: CallbackOptions) -> Iterable[Observation]:  # noqa: ARG001
    """Observe one size per store file, or nothing when the JMX bean did not answer."""
    try:
        snapshot = _neo4j_snapshot()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not observe %s", NEO4J_STORE_SIZE_BYTES, exc_info=True)
        return []
    if snapshot is None:
        return []
    return [Observation(size, {"store": store}) for store, size in snapshot.store_sizes.items()]


def register_neo4j_gauges(driver: Any) -> None:
    """Point the Neo4j gauges at `driver`, registering them against the current provider.

    Call it from the console's running event loop once telemetry is configured — the callbacks
    submit their work back to whichever loop is running here. Calling it again swaps the driver
    without re-registering the instruments, so a restart inside one process cannot make the SDK
    complain about duplicates or leave a second set of callbacks reading a closed driver.
    """
    global _neo4j_source, _neo4j_gauge_generation

    loop = asyncio.get_running_loop()
    generation = provider_generation()
    with _lock:
        _neo4j_source = _Neo4jGaugeSource(driver, loop)
        if _neo4j_gauge_generation == generation:
            return
        _neo4j_gauge_generation = generation

    meter = get_meter(INSTRUMENTATION_SCOPE)
    meter.create_observable_gauge(NEO4J_UP, callbacks=[_observe_up], description="1 when Neo4j answered the last gauge refresh, 0 when it did not.")
    meter.create_observable_gauge(NEO4J_NODES, callbacks=[_observe_nodes], description="Number of nodes carrying each schema label.")
    meter.create_observable_gauge(NEO4J_RELATIONSHIPS, callbacks=[_observe_relationships], description="Number of relationships of each schema type.")
    meter.create_observable_gauge(
        NEO4J_TRANSACTIONS_ACTIVE,
        callbacks=[_observe_transactions],
        description="Transactions Neo4j currently reports, including this reading's own.",
    )
    meter.create_observable_gauge(
        NEO4J_STORE_SIZE_BYTES, callbacks=[_observe_store_sizes], unit="By", description="On-disk size of each Neo4j store file, when JMX reports it."
    )


def reset_neo4j_gauges() -> None:
    """Detach the Neo4j gauges from their driver so the callbacks observe nothing.

    Used on shutdown, when the driver is about to close, and as the tests' seam. The
    instruments themselves stay registered — the SDK has no way to remove one — but with no
    source behind them they report nothing rather than a fabricated up=0 for a console that is
    simply no longer running.
    """
    global _neo4j_source, _neo4j_gauge_generation

    with _lock:
        _neo4j_source = None
        _neo4j_gauge_generation = -1
