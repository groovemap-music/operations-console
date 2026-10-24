"""Transition detection and the bounded ring behind the console's event feed."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from dashboard.dashboard import DashboardApp, DatabaseInfo, PipelineMetrics, QueueInfo, ServiceStatus, SystemMetrics
from dashboard.events import EVENT_RING_SIZE, EventDetector


DLQ = "acme-discogs-graphinator-artists.dlq"
WORK = "acme-discogs-graphinator-artists"
_CLOCK = [datetime(2026, 1, 1, tzinfo=UTC)]


def _service(name: str, status: str = "healthy") -> ServiceStatus:
    return ServiceStatus(name=name, status=status, last_seen=None, current_task=None, progress=None, error=None)


def _queue(name: str, messages: int = 0, consumers: int = 1, ack_rate: float | None = None) -> QueueInfo:
    return QueueInfo(
        name=name, messages=messages, messages_ready=messages, messages_unacknowledged=0, consumers=consumers, message_rate=None, ack_rate=ack_rate
    )


def _reading(
    *,
    services: dict[str, str] | None = None,
    queues: list[QueueInfo] | None = None,
    available: bool = True,
    databases: dict[str, str] | None = None,
) -> SystemMetrics:
    _CLOCK[0] += timedelta(seconds=2)
    return SystemMetrics(
        pipelines={
            "discogs": PipelineMetrics(
                services=[_service(name, status) for name, status in (services or {"graphinator": "healthy"}).items()],
                queues=queues or [],
                queues_available=available,
            )
        },
        databases=[
            DatabaseInfo(name=name, status=status, connection_count=1, size=None, error=None)
            for name, status in (databases or {"Neo4j": "healthy"}).items()
        ],
        timestamp=_CLOCK[0],
    )


def _kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["kind"] for event in events]


def _detector() -> EventDetector:
    return EventDetector(dead_letter_queues=frozenset({DLQ}))


class TestBaseline:
    def test_the_first_reading_is_a_baseline_not_news(self) -> None:
        detector = _detector()
        events = detector.observe(_reading(services={"graphinator": "unhealthy"}, databases={"Neo4j": "error"}, queues=[_queue(DLQ, 40)]))
        assert events == []
        assert detector.backlog() == []

    def test_an_unchanged_reading_adds_nothing(self) -> None:
        detector = _detector()
        detector.observe(_reading())
        assert detector.observe(_reading()) == []


class TestServices:
    def test_healthy_to_unhealthy_and_back(self) -> None:
        detector = _detector()
        detector.observe(_reading())
        down = detector.observe(_reading(services={"graphinator": "unhealthy"}))
        assert [(e["kind"], e["level"], e["message"]) for e in down] == [("service-down", "error", "graphinator is unhealthy")]
        up = detector.observe(_reading())
        assert [(e["kind"], e["level"]) for e in up] == [("service-recovered", "info")]

    def test_unknown_is_a_warning_and_unhealthy_to_unknown_is_not_a_new_event(self) -> None:
        detector = _detector()
        detector.observe(_reading())
        assert [e["level"] for e in detector.observe(_reading(services={"graphinator": "unknown"}))] == ["warning"]
        assert detector.observe(_reading(services={"graphinator": "unhealthy"})) == []

    def test_starting_and_extracting_count_as_serving(self) -> None:
        detector = _detector()
        detector.observe(_reading(services={"graphinator": "unhealthy"}))
        assert _kinds(detector.observe(_reading(services={"graphinator": "starting"}))) == ["service-recovered"]
        assert detector.observe(_reading(services={"graphinator": "extracting"})) == []


class TestDatastores:
    def test_up_to_down_and_back(self) -> None:
        detector = _detector()
        detector.observe(_reading())
        assert _kinds(detector.observe(_reading(databases={"Neo4j": "unhealthy"}))) == ["datastore-down"]
        assert _kinds(detector.observe(_reading())) == ["datastore-up"]


class TestBrokerAvailability:
    def test_true_to_false_and_back(self) -> None:
        detector = _detector()
        detector.observe(_reading())
        assert _kinds(detector.observe(_reading(available=False))) == ["queues-unavailable"]
        assert _kinds(detector.observe(_reading())) == ["queues-available"]


class TestDeadLetterQueues:
    def test_growth_and_emptying_are_reported_with_the_change(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(DLQ, 3)]))
        grew = detector.observe(_reading(queues=[_queue(DLQ, 10)]))
        assert [(e["kind"], e["level"], e["message"]) for e in grew] == [("dlq-grew", "error", f"{DLQ} grew by 7 messages to 10")]
        assert detector.observe(_reading(queues=[_queue(DLQ, 4)])) == []  # draining is not yet empty
        assert _kinds(detector.observe(_reading(queues=[_queue(DLQ, 0)]))) == ["dlq-emptied"]

    def test_growth_by_one_is_singular(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(DLQ, 0)]))
        assert "grew by 1 message to 1" in detector.observe(_reading(queues=[_queue(DLQ, 1)]))[0]["message"]

    def test_depth_is_not_compared_across_a_broker_outage(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(DLQ, 3)]))
        assert _kinds(detector.observe(_reading(available=False))) == ["queues-unavailable"]
        assert _kinds(detector.observe(_reading(queues=[_queue(DLQ, 90)]))) == ["queues-available"]

    def test_work_queue_depth_and_consumers_are_not_events(self) -> None:
        """Queue stall and no-consumer rules live in the browser; the server never re-derives them."""
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5, consumers=1, ack_rate=3.0)]))
        for _ in range(60):
            assert detector.observe(_reading(queues=[_queue(WORK, 90_000, consumers=0, ack_rate=0.0)])) == []


class TestRing:
    def test_events_carry_increasing_ids_and_the_reading_time(self) -> None:
        detector = _detector()
        detector.observe(_reading())
        reading = _reading(services={"graphinator": "unhealthy"}, databases={"Neo4j": "unhealthy"})
        events = detector.observe(reading)
        assert [e["id"] for e in events] == [1, 2]
        assert all(e["timestamp"] == reading.timestamp.isoformat() for e in events)

    def test_the_ring_keeps_only_the_newest_events(self) -> None:
        detector = _detector()
        detector.observe(_reading())
        for i in range(EVENT_RING_SIZE + 25):
            detector.observe(_reading(databases={"Neo4j": "unhealthy" if i % 2 == 0 else "healthy"}))
        backlog = detector.backlog()
        assert len(backlog) == EVENT_RING_SIZE
        assert backlog[-1]["id"] == EVENT_RING_SIZE + 25
        assert backlog[0]["id"] == 26
        assert [e["id"] for e in backlog] == sorted(e["id"] for e in backlog)

    def test_a_small_capacity_is_honoured(self) -> None:
        detector = EventDetector(capacity=2)
        detector.observe(_reading())
        for i in range(5):
            detector.observe(_reading(databases={"Neo4j": "unhealthy" if i % 2 == 0 else "healthy"}))
        assert len(detector.backlog()) == 2


class TestCollectionLoop:
    def test_the_loop_records_and_broadcasts_a_transition(self) -> None:
        async def run() -> list[str]:
            app = DashboardApp.__new__(DashboardApp)
            app.events = _detector()
            app.websockets = MagicMock()
            app.websockets.connections = {object()}
            app.websockets.broadcast = AsyncMock()
            app.events.observe(_reading())
            await app.broadcast_events(app.events.observe(_reading(databases={"Neo4j": "unhealthy"})))
            return [call.args[0] for call in app.websockets.broadcast.await_args_list]

        sent = asyncio.run(run())
        assert len(sent) == 1
        assert '"type":"event"' in sent[0]
        assert "datastore-down" in sent[0]
