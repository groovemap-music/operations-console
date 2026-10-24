"""Prove the live rates are real against a RabbitMQ 4.x broker with management stats collection off.

Skipped unless a broker is named, so it never runs in CI. To run it locally, start a broker with
the Prometheus plugin and `management_agent.disable_metrics_collector = true`, then:

    GROOVEMAP_RABBITMQ_PROMETHEUS_URL=http://127.0.0.1:25692 \
    GROOVEMAP_RABBITMQ_AMQP_URL=amqp://guest:guest@127.0.0.1:25672/%2F pytest tests/test_queue_metrics_integration.py
"""

import os
import time
import uuid
from typing import Any
from unittest.mock import Mock, patch

import pytest

from dashboard.dashboard import DashboardApp


_PROMETHEUS_URL = os.environ.get("GROOVEMAP_RABBITMQ_PROMETHEUS_URL", "")
_AMQP_URL = os.environ.get("GROOVEMAP_RABBITMQ_AMQP_URL", "")


@pytest.mark.integration
@pytest.mark.skipif(
    not (_PROMETHEUS_URL and _AMQP_URL),
    reason="set GROOVEMAP_RABBITMQ_PROMETHEUS_URL and GROOVEMAP_RABBITMQ_AMQP_URL to read live rates from a real broker",
)
class TestLiveRatesAgainstRealBroker:
    @pytest.mark.asyncio
    async def test_publish_and_ack_produce_non_zero_rates(self) -> None:
        import pika

        prefix = f"console-it-{uuid.uuid4().hex[:8]}"
        queue = f"{prefix}-queue"
        connection = pika.BlockingConnection(pika.URLParameters(_AMQP_URL))
        channel: Any = connection.channel()
        channel.exchange_declare(prefix, "fanout", auto_delete=True)
        channel.queue_declare(queue, durable=True)
        channel.queue_bind(queue, prefix)
        try:
            with patch("dashboard.dashboard.get_config", return_value=Mock(rabbitmq_prometheus_url=_PROMETHEUS_URL)):
                app = DashboardApp()

            await self._poll_until_queue_is_scraped(app, prefix)
            for _ in range(50):
                channel.basic_publish(prefix, "", b"x")
            for _ in range(30):
                method, _, _ = channel.basic_get(queue)
                channel.basic_ack(method.delivery_tag)

            info = await self._poll_until(app, prefix, lambda q: q.message_rate > 0 and q.ack_rate > 0)
            assert info.message_rate > 0
            assert info.ack_rate > 0

            # Depth gauges are refreshed on the broker's stats interval, so wait for them to settle.
            settled = await self._poll_until(app, prefix, lambda q: q.messages == 20)
            assert (settled.messages_ready, settled.messages_unacknowledged) == (20, 0)
        finally:
            channel.queue_delete(queue)
            connection.close()

    @staticmethod
    async def _poll_until_queue_is_scraped(app: DashboardApp, prefix: str) -> None:
        await TestLiveRatesAgainstRealBroker._poll_until(app, prefix, lambda _q: True)

    @staticmethod
    async def _poll_until(app: DashboardApp, prefix: str, done: Any) -> Any:
        """Poll like the console loop (2s apart) until the queue appears and `done` holds."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            queues = await app.get_queue_info(prefix)
            assert queues is not None, "broker scrape failed"
            if queues and done(queues[0]):
                return queues[0]
            time.sleep(2)
        raise AssertionError("queue never reported the expected rates")
