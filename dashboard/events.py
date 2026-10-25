"""Operator-facing event feed: state transitions the console server observes between polls.

Every poll of the collection loop is compared with the previous one, and each change an operator
cares about becomes one event in a bounded in-memory ring: a service going healthy or unhealthy, a
datastore going up or down, the broker scrape failing or recovering, a dead-letter queue growing or
emptying. A reconnecting client receives the ring as its backlog, so every operator sees the same
history.

The queue health rules (no consumer, stalled) live here too, so every operator sees the same
verdict and the same events, and a stall is detected while no browser is open. Each work queue's
current verdict is also written to ``QueueInfo.condition``, which the matrix and the attention bar
render as given.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from dashboard.dashboard import QueueCondition, SystemMetrics


# The most events the ring keeps; the oldest is dropped first.
EVENT_RING_SIZE = 200

LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

# A service in either status is not serving; every other status (healthy, starting, extracting) is.
_SERVICE_DOWN = frozenset({"unhealthy", "unknown", "unreachable"})

# The platform group whose outage is a warning rather than an error: the stack serves without it.
_OBSERVABILITY = "observability"


# A work queue with a backlog, consumers and computed zero acks for this long is stalled.
STALL_SECONDS = 60
# The stall label stops counting at this span ("5m+").
STALL_LABEL_CAP_SECONDS = 300
# A longer silence between readings starts a queue's zero-ack run afresh.
GAP_SECONDS = 30

KIND_NO_CONSUMER = "no-consumer"
KIND_STALLED = "stalled"


def _duration(seconds: float) -> str:
    """The short form the matrix has always used: 45s, 3m."""
    return f"{max(1, round(seconds))}s" if seconds < 60 else f"{round(seconds / 60)}m"


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
    work_queues: frozenset[str] = frozenset()
    capacity: int = EVENT_RING_SIZE
    ring: deque[dict[str, Any]] = field(init=False)
    _next_id: int = field(default=1, init=False)
    _services: dict[tuple[str, str], bool] = field(default_factory=dict, init=False)
    _platform: dict[str, bool] = field(default_factory=dict, init=False)
    _datastores: dict[str, bool] = field(default_factory=dict, init=False)
    _available: dict[str, bool] = field(default_factory=dict, init=False)
    _dlq_depth: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    # Per work queue: when the current run of computed zero acks with a backlog began, the time of
    # the last reading, and the condition last reported (None while it is healthy).
    _zero_since: dict[tuple[str, str], datetime] = field(default_factory=dict, init=False)
    _seen_at: dict[tuple[str, str], datetime] = field(default_factory=dict, init=False)
    _conditions: dict[tuple[str, str], str | None] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Size the ring."""
        self.ring = deque(maxlen=self.capacity)

    def backlog(self) -> list[dict[str, Any]]:
        """The retained events, oldest first."""
        return list(self.ring)

    def observe(self, metrics: SystemMetrics) -> list[dict[str, Any]]:
        """Compare one reading with the last; record and return the events it produced.

        Each work queue of the reading also gets its current ``condition`` set.
        """
        found: list[tuple[str, str, str, str]] = []  # (level, kind, subject, message)
        for pipeline_id, pipeline in metrics.pipelines.items():
            self._services_of(pipeline_id, pipeline.services, found)
            self._broker_of(pipeline_id, pipeline, found)
            self._work_queues_of(pipeline_id, pipeline, metrics.timestamp, found)
        self._platform_of(metrics.platform, found)
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

    def _platform_of(self, platform: list[Any], found: list[tuple[str, str, str, str]]) -> None:
        """Platform services; a datastore row is left to ``_datastores_of``, which reports it once."""
        for service in platform:
            if service.source != "probe":
                continue
            serving = service.status not in _SERVICE_DOWN
            previous = self._platform.get(service.name)
            self._platform[service.name] = serving
            if previous is None or previous == serving:
                continue
            if serving:
                found.append((LEVEL_INFO, "service-recovered", service.name, f"{service.name} is {service.status}"))
            else:
                level = LEVEL_WARNING if service.group == _OBSERVABILITY else LEVEL_ERROR
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

    def _work_queues_of(self, pipeline_id: str, pipeline: Any, at: datetime, found: list[tuple[str, str, str, str]]) -> None:
        """Raise and clear the no-consumer and stalled conditions of each work queue.

        No consumer: a backlog and no consumer, at once. Stalled: a backlog, consumers, and a
        computed zero ack rate for ``STALL_SECONDS``, measured from the first reading of the run
        (conservative by up to one poll). An unknown ack rate is not a zero and ends the run, as
        does a broker scrape failure or a gap of more than ``GAP_SECONDS`` between readings.
        """
        if not pipeline.queues_available:
            for state in (self._zero_since, self._seen_at, self._conditions):
                for key in [key for key in state if key[0] == pipeline_id]:
                    del state[key]
            return
        for queue in pipeline.queues:
            if queue.name not in self.work_queues:
                continue
            key = (pipeline_id, queue.name)
            last = self._seen_at.get(key)
            if last is not None and (at - last).total_seconds() > GAP_SECONDS:
                self._zero_since.pop(key, None)
            self._seen_at[key] = at
            if queue.messages > 0 and queue.ack_rate == 0:
                self._zero_since.setdefault(key, at)
            else:
                self._zero_since.pop(key, None)
            condition = self._condition_of(queue, (at - self._zero_since[key]).total_seconds() if key in self._zero_since else 0)
            queue.condition = condition
            kind = condition.kind if condition else None
            known = key in self._conditions
            previous = self._conditions.get(key)
            self._conditions[key] = kind
            if not known or previous == kind:
                continue
            if condition is not None:
                found.append((LEVEL_ERROR, f"queue-{kind}", queue.name, f"{queue.name}: {condition.label}"))
            else:
                found.append((LEVEL_INFO, "queue-recovered", queue.name, f"{queue.name} is {'drained' if queue.messages == 0 else 'flowing'}"))

    @staticmethod
    def _condition_of(queue: Any, zero_seconds: float) -> QueueCondition | None:
        from dashboard.dashboard import QueueCondition  # noqa: PLC0415 - dashboard imports this module

        if queue.messages > 0 and queue.consumers == 0:
            return QueueCondition(kind=KIND_NO_CONSUMER, label="NO CONSUMER")
        if queue.messages > 0 and queue.consumers > 0 and zero_seconds >= STALL_SECONDS:
            span = f"{_duration(STALL_LABEL_CAP_SECONDS)}+" if zero_seconds >= STALL_LABEL_CAP_SECONDS else _duration(zero_seconds)
            return QueueCondition(kind=KIND_STALLED, label=f"STALLED · no acks {span}")
        return None
