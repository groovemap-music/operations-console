"""Operator-facing event feed: state transitions the console server observes between polls.

Every poll of the collection loop is compared with the previous one, and each change an operator
cares about becomes one event in a bounded in-memory ring: a service going healthy or unhealthy, a
datastore going up or down, the broker scrape failing or recovering, a dead-letter queue growing or
emptying. A reconnecting client receives the ring as its backlog, so every operator sees the same
history.

The queue health rules (no consumer, stalled) are deliberately not re-derived here. The matrix and
the attention bar compute them in the browser (``QueueHealth`` in ``static/dashboard.js``), and a
second copy would drift. The server reports only what it observes directly.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from dashboard.dashboard import SystemMetrics


# The most events the ring keeps; the oldest is dropped first.
EVENT_RING_SIZE = 200

LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

# A service in either status is not serving; every other status (healthy, starting, extracting) is.
_SERVICE_DOWN = frozenset({"unhealthy", "unknown"})


def _plural(count: int) -> str:
    return f"{count} message" if count == 1 else f"{count} messages"


@dataclass
class EventDetector:
    """Detect transitions between consecutive readings and keep the recent ones in a ring.

    The first reading of anything only sets a baseline, so a console restart does not announce
    the state of the whole system as news. A dead-letter queue is not compared across a broker
    scrape outage: its depth starts afresh when the scrape recovers.
    """

    dead_letter_queues: frozenset[str] = frozenset()
    capacity: int = EVENT_RING_SIZE
    ring: deque[dict[str, Any]] = field(init=False)
    _next_id: int = field(default=1, init=False)
    _services: dict[tuple[str, str], bool] = field(default_factory=dict, init=False)
    _datastores: dict[str, bool] = field(default_factory=dict, init=False)
    _available: dict[str, bool] = field(default_factory=dict, init=False)
    _dlq_depth: dict[tuple[str, str], int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Size the ring."""
        self.ring = deque(maxlen=self.capacity)

    def backlog(self) -> list[dict[str, Any]]:
        """The retained events, oldest first."""
        return list(self.ring)

    def observe(self, metrics: SystemMetrics) -> list[dict[str, Any]]:
        """Compare one reading with the last; record and return the events it produced."""
        found: list[tuple[str, str, str, str]] = []  # (level, kind, subject, message)
        for pipeline_id, pipeline in metrics.pipelines.items():
            self._services_of(pipeline_id, pipeline.services, found)
            self._broker_of(pipeline_id, pipeline, found)
        self._datastores_of(metrics, found)
        stamp = metrics.timestamp.astimezone(UTC).isoformat()
        events = []
        for level, kind, subject, message in found:
            event = {"id": self._next_id, "timestamp": stamp, "level": level, "kind": kind, "subject": subject, "message": message}
            self._next_id += 1
            self.ring.append(event)
            events.append(event)
        return events

    def _services_of(self, pipeline_id: str, services: list[Any], found: list[tuple[str, str, str, str]]) -> None:
        for service in services:
            key = (pipeline_id, service.name)
            serving = service.status not in _SERVICE_DOWN
            previous = self._services.get(key)
            self._services[key] = serving
            if previous is None or previous == serving:
                continue
            if serving:
                found.append((LEVEL_INFO, "service-recovered", service.name, f"{service.name} is {service.status}"))
            else:
                level = LEVEL_ERROR if service.status == "unhealthy" else LEVEL_WARNING
                found.append((level, "service-down", service.name, f"{service.name} is {service.status}"))

    def _datastores_of(self, metrics: SystemMetrics, found: list[tuple[str, str, str, str]]) -> None:
        for database in metrics.databases:
            up = database.status == "healthy"
            previous = self._datastores.get(database.name)
            self._datastores[database.name] = up
            if previous is None or previous == up:
                continue
            if up:
                found.append((LEVEL_INFO, "datastore-up", database.name, f"{database.name} is up"))
            else:
                found.append((LEVEL_ERROR, "datastore-down", database.name, f"{database.name} is down"))

    def _broker_of(self, pipeline_id: str, pipeline: Any, found: list[tuple[str, str, str, str]]) -> None:
        available = pipeline.queues_available
        previous = self._available.get(pipeline_id)
        self._available[pipeline_id] = available
        if previous is not None and previous != available:
            if available:
                found.append((LEVEL_INFO, "queues-available", pipeline_id, f"{pipeline_id}: queue metrics are available again"))
            else:
                found.append((LEVEL_WARNING, "queues-unavailable", pipeline_id, f"{pipeline_id}: queue metrics unavailable"))
        if not available:
            self._dlq_depth = {key: depth for key, depth in self._dlq_depth.items() if key[0] != pipeline_id}
            return
        for queue in pipeline.queues:
            if queue.name not in self.dead_letter_queues:
                continue
            key = (pipeline_id, queue.name)
            before = self._dlq_depth.get(key)
            self._dlq_depth[key] = queue.messages
            if before is None or before == queue.messages:
                continue
            if queue.messages > before:
                found.append((LEVEL_ERROR, "dlq-grew", queue.name, f"{queue.name} grew by {_plural(queue.messages - before)} to {queue.messages}"))
            elif queue.messages == 0:
                found.append((LEVEL_INFO, "dlq-emptied", queue.name, f"{queue.name} emptied"))
