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
    return EventDetector(dead_letter_queues=frozenset({DLQ}), work_queues=frozenset({WORK}))


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


class TestWorkQueueConditions:
    """No consumer and stalled, with the thresholds the matrix has always used (60 s, 5m cap, 30 s gap)."""

    def _stalled(self, detector: EventDetector, readings: int, **extra: Any) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for _ in range(readings):
            events += detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=0.0, **extra)]))
        return events

    def test_a_backlog_with_no_consumer_is_raised_and_cleared(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5, consumers=1, ack_rate=3.0)]))
        raised = detector.observe(_reading(queues=[_queue(WORK, 90_000, consumers=0, ack_rate=3.0)]))
        assert [(e["kind"], e["level"], e["message"]) for e in raised] == [("queue-no-consumer", "error", f"{WORK}: NO CONSUMER")]
        assert detector.observe(_reading(queues=[_queue(WORK, 90_000, consumers=0, ack_rate=3.0)])) == []  # once, not every poll
        cleared = detector.observe(_reading(queues=[_queue(WORK, 90_000, consumers=1, ack_rate=3.0)]))
        assert [(e["kind"], e["level"], e["message"]) for e in cleared] == [("queue-recovered", "info", f"{WORK} is flowing")]

    def test_an_empty_queue_has_no_consumer_problem_and_a_drain_says_drained(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 0, consumers=0)]))
        assert detector.observe(_reading(queues=[_queue(WORK, 0, consumers=0)])) == []
        detector.observe(_reading(queues=[_queue(WORK, 4, consumers=0)]))
        assert detector.observe(_reading(queues=[_queue(WORK, 0, consumers=0)]))[0]["message"] == f"{WORK} is drained"

    def test_a_stall_needs_sixty_seconds_of_computed_zero_acks(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=3.0)]))  # baseline
        assert self._stalled(detector, 30) == []  # 29 intervals of 2 s: 58 s
        raised = self._stalled(detector, 1)
        assert [(e["kind"], e["level"], e["message"]) for e in raised] == [("queue-stalled", "error", f"{WORK}: STALLED · no acks 1m")]
        assert self._stalled(detector, 5) == []

    def test_the_stall_clears_when_acks_resume(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=3.0)]))
        self._stalled(detector, 31)
        assert _kinds(detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=4.0)]))) == ["queue-recovered"]

    def test_an_unknown_ack_rate_is_never_a_zero_and_restarts_the_run(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=3.0)]))
        for _ in range(60):
            assert detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=None)])) == []
        self._stalled(detector, 20)
        detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=None)]))
        assert self._stalled(detector, 30) == []  # the window started again after the unknown rate

    def test_a_stall_needs_a_backlog_and_a_consumer(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 0, ack_rate=3.0)]))
        for _ in range(40):
            assert detector.observe(_reading(queues=[_queue(WORK, 0, ack_rate=0.0)])) == []

    def test_the_label_counts_up_and_stops_at_five_minutes(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=3.0)]))
        self._stalled(detector, 31)
        reading = _reading(queues=[_queue(WORK, 5000, ack_rate=0.0)])
        for _ in range(200):
            reading = _reading(queues=[_queue(WORK, 5000, ack_rate=0.0)])
            detector.observe(reading)
        assert reading.pipelines["discogs"].queues[0].condition.label == "STALLED · no acks 5m+"  # type: ignore[union-attr]

    def test_a_gap_between_readings_starts_the_run_afresh(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=3.0)]))
        self._stalled(detector, 20)
        _CLOCK[0] += timedelta(seconds=45)
        assert self._stalled(detector, 20) == []  # 40 s since the gap, not 80 s

    def test_a_broker_outage_ends_the_run_and_does_not_announce_a_recovery(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=3.0)]))
        self._stalled(detector, 31)
        assert _kinds(detector.observe(_reading(available=False))) == ["queues-unavailable"]
        assert _kinds(detector.observe(_reading(queues=[_queue(WORK, 5000, ack_rate=0.0)]))) == ["queues-available"]
        assert self._stalled(detector, 20) == []

    def test_the_first_reading_is_a_baseline_but_still_sets_the_condition(self) -> None:
        detector = _detector()
        reading = _reading(queues=[_queue(WORK, 5000, consumers=0)])
        assert detector.observe(reading) == []
        assert reading.pipelines["discogs"].queues[0].condition.kind == "no-consumer"  # type: ignore[union-attr]

    def test_a_queue_that_is_not_a_work_queue_raises_nothing(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue("unrelated", 5, ack_rate=3.0)]))
        assert detector.observe(_reading(queues=[_queue("unrelated", 9000, consumers=0)])) == []

    def test_the_condition_is_part_of_the_reading_every_client_receives(self) -> None:
        detector = _detector()
        reading = _reading(queues=[_queue(WORK, 9, consumers=0), _queue(DLQ, 1)])
        detector.observe(reading)
        queues = reading.model_dump()["pipelines"]["discogs"]["queues"]
        assert queues[0]["condition"] == {"kind": "no-consumer", "label": "NO CONSUMER"}
        assert queues[1]["condition"] is None

    def test_the_events_stay_in_the_ring_for_a_reconnecting_operator(self) -> None:
        detector = _detector()
        detector.observe(_reading(queues=[_queue(WORK, 5, ack_rate=3.0)]))
        detector.observe(_reading(queues=[_queue(WORK, 9, consumers=0)]))
        assert _kinds(detector.backlog()) == ["queue-no-consumer"]


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
