"""Per-queue live metrics read from the RabbitMQ Prometheus plugin's /metrics/detailed endpoint.

The broker's deprecated management statistics no longer serve the live rates; the Prometheus
plugin is the supported source. It exposes only counters for publish and ack traffic, so the
per-second rates the live view shows are computed here from the delta between consecutive polls.
"""

import re
from dataclasses import dataclass, field

from prometheus_client.parser import text_string_to_metric_families


QUEUE_FAMILIES = ("queue_coarse_metrics", "queue_consumer_count", "queue_delivery_metrics", "queue_exchange_metrics")

_MESSAGES = "rabbitmq_detailed_queue_messages"
_READY = "rabbitmq_detailed_queue_messages_ready"
_UNACKED = "rabbitmq_detailed_queue_messages_unacked"
_CONSUMERS = "rabbitmq_detailed_queue_consumers"
_ACKED = "rabbitmq_detailed_queue_messages_acked_total"
_PUBLISHED = "rabbitmq_detailed_queue_exchange_messages_published_total"

_WANTED = {_MESSAGES, _READY, _UNACKED, _CONSUMERS, _ACKED, _PUBLISHED}

QueueKey = tuple[str, str]  # (vhost, queue name)


def queue_name_regex(prefix: str) -> str:
    """Anchored queue-name regex for the Prometheus ``queue`` parameter, built from a contract prefix."""
    return f"^{re.escape(prefix)}"


def scrape_params(prefix: str) -> list[tuple[str, str | int | float | bool | None]]:
    """Query parameters for one /metrics/detailed scrape of the queues under ``prefix``."""
    return [*(("family", family) for family in QUEUE_FAMILIES), ("queue", queue_name_regex(prefix))]


@dataclass
class QueueSample:
    """Gauges and cumulative counters for one queue from a single scrape."""

    messages: int = 0
    messages_ready: int = 0
    messages_unacknowledged: int = 0
    consumers: int = 0
    published_total: float = 0.0
    acked_total: float = 0.0


def parse_queue_samples(text: str) -> dict[QueueKey, QueueSample]:
    """Parse a /metrics/detailed body into one sample per (vhost, queue).

    Publish counters carry an ``exchange`` label and are summed across exchanges. A counter series
    appears only after its first event, so a queue with none yet keeps a total of zero.
    """
    samples: dict[QueueKey, QueueSample] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name not in _WANTED:
                continue
            key = (sample.labels.get("vhost", ""), sample.labels["queue"])
            queue = samples.setdefault(key, QueueSample())
            if sample.name == _MESSAGES:
                queue.messages = int(sample.value)
            elif sample.name == _READY:
                queue.messages_ready = int(sample.value)
            elif sample.name == _UNACKED:
                queue.messages_unacknowledged = int(sample.value)
            elif sample.name == _CONSUMERS:
                queue.consumers = int(sample.value)
            elif sample.name == _ACKED:
                queue.acked_total += sample.value
            else:
                queue.published_total += sample.value
    return samples


@dataclass
class RateTracker:
    """Turn consecutive counter samples into per-second publish and ack rates.

    A queue seen for the first time, or again after missing a poll, has no baseline and reports no
    rate. A counter that goes down is a broker or queue reset and yields no rate for that interval.
    """

    _previous: dict[QueueKey, tuple[float, QueueSample]] = field(default_factory=dict)

    def update(self, samples: dict[QueueKey, QueueSample], now: float) -> dict[QueueKey, tuple[float, float]]:
        """Record a scrape taken at monotonic time ``now`` and return (publish, ack) rates per queue."""
        rates: dict[QueueKey, tuple[float, float]] = {}
        for key, sample in samples.items():
            baseline = self._previous.get(key)
            if baseline is None or now <= baseline[0]:
                rates[key] = (0.0, 0.0)
                continue
            elapsed = now - baseline[0]
            rates[key] = (
                _rate(baseline[1].published_total, sample.published_total, elapsed),
                _rate(baseline[1].acked_total, sample.acked_total, elapsed),
            )
        self._previous = {key: (now, sample) for key, sample in samples.items()}
        return rates


def _rate(before: float, after: float, elapsed: float) -> float:
    if after < before:
        return 0.0
    return (after - before) / elapsed
