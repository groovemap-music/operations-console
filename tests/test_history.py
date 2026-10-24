"""Queue Trends and System Health history: the PromQL, the step per range, and the failure state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import history
from tests.dashboard_test_app import create_test_app


if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.fake_catalog_api import FakeCatalogApi


QUEUE = "groovemap-discogs-graphinator-artists"
DLQ = "groovemap-discogs-graphinator-artists.dlq"

STEPS = {"1h": ("30s", "1m"), "6h": ("2m", "2m"), "24h": ("5m", "5m"), "7d": ("30m", "30m"), "30d": ("2h", "2h"), "90d": ("6h", "6h")}
SECONDS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000, "90d": 7776000}

MATCHER = 'queue=~"(groovemap\\\\-discogs|groovemap\\\\-musicbrainz).*"'


class FakeVictoriaMetrics:
    """Records every request and answers from a query-substring -> result table."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list[tuple[str, str, list[dict[str, Any]]]] = []  # (path, query substring, result)
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="boom")
        query = dict(request.url.params)["query"]
        for path, needle, result in self.answers:
            if request.url.path == path and needle in query:
                return httpx.Response(200, json={"status": "success", "data": {"resultType": "x", "result": result}})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "x", "result": []}})

    def params(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests]


@pytest.fixture
def vm(monkeypatch: pytest.MonkeyPatch) -> FakeVictoriaMetrics:
    fake = FakeVictoriaMetrics()
    monkeypatch.setattr(history, "_transport", httpx.MockTransport(fake))
    monkeypatch.delenv("VICTORIA_METRICS_URL", raising=False)
    return fake


@pytest.fixture
def client(fake_catalog: FakeCatalogApi) -> Iterator[TestClient]:
    with TestClient(create_test_app(), headers={"Authorization": f"Bearer {fake_catalog.mint()}"}) as test_client:
        yield test_client


def _series(label: str, name: str, values: list[tuple[int, str]]) -> dict[str, Any]:
    return {"metric": {label: name}, "values": [[ts, v] for ts, v in values]}


class TestQueries:
    def test_queue_queries_are_pinned(self) -> None:
        assert history.queue_queries("5m") == {
            "messages_ready": f"sum by (queue) (rabbitmq_queue_messages_ready{{{MATCHER}}})",
            "messages_unacknowledged": f"sum by (queue) (rabbitmq_queue_messages_unacked{{{MATCHER}}})",
            "messages_total": f"sum by (queue) (rabbitmq_queue_messages{{{MATCHER}}})",
            "consumers": f"sum by (queue) (rabbitmq_queue_consumers{{{MATCHER}}})",
            "publish_rate": f"sum by (queue) (rate(rabbitmq_detailed_queue_exchange_messages_published_total{{{MATCHER}}}[5m]))",
            "ack_rate": f"sum by (queue) (rate(rabbitmq_detailed_queue_messages_acked_total{{{MATCHER}}}[5m]))",
        }

    def test_health_queries_are_pinned(self) -> None:
        q = history.health_queries("5m")
        sel = 'target!="loop"'
        assert q["availability"] == (
            f'sum by (target) (increase(groovemap_console_poll_duration_seconds_count{{{sel},outcome="success"}}[5m]))'
            f" / sum by (target) (increase(groovemap_console_poll_duration_seconds_count{{{sel}}}[5m]))"
        )
        assert q["response_time_ms"] == (
            f'1000 * sum by (target) (rate(groovemap_console_poll_duration_seconds_sum{{{sel},outcome="success"}}[5m]))'
            f' / sum by (target) (rate(groovemap_console_poll_duration_seconds_count{{{sel},outcome="success"}}[5m]))'
        )
        for label, quantile in (("p50", "0.5"), ("p95", "0.95"), ("p99", "0.99")):
            assert q[label] == (
                f"1000 * histogram_quantile({quantile}, sum by (le) "
                f'(rate(groovemap_console_poll_duration_seconds_bucket{{{sel},outcome="success"}}[5m])))'
            )

    def test_endpoint_queries_are_pinned(self) -> None:
        q = history.endpoint_queries("7d")
        assert q["request_count"] == "topk(50, sum by (service_name, http_route) (increase(http_server_request_duration_seconds_count[7d])))"
        assert q["error_count"] == (
            'sum by (service_name, http_route) (increase(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[7d]))'
        )
        assert q["p95"] == (
            "1000 * histogram_quantile(0.95, sum by (le, service_name, http_route) (increase(http_server_request_duration_seconds_bucket[7d])))"
        )

    def test_the_ranges_are_exactly_the_six_offered(self) -> None:
        assert list(history.RANGES) == list(STEPS)


class TestQueueRoute:
    @pytest.mark.parametrize("range_name", list(STEPS))
    def test_each_range_queries_with_its_step_and_window(self, client: TestClient, vm: FakeVictoriaMetrics, range_name: str) -> None:
        response = client.get(f"/api/history/queues?range={range_name}")
        assert response.status_code == 200
        step, window = STEPS[range_name]
        assert response.json()["range"] == range_name
        assert response.json()["step"] == step
        sent = vm.params()
        assert len(sent) == 6
        for request, params in zip(vm.requests, sent, strict=True):
            assert request.url.path == "/api/v1/query_range"
            assert request.url.host == "victoria-metrics"
            assert params["step"] == step
            assert int(params["end"]) - int(params["start"]) == SECONDS[range_name]
        rates = [p["query"] for p in sent if "rate(" in p["query"]]
        assert len(rates) == 2
        assert all(f"[{window}]" in q for q in rates)

    def test_default_range_is_24h_and_unknown_ranges_are_rejected(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        assert client.get("/api/history/queues").json()["range"] == "24h"
        assert client.get("/api/history/queues?range=2h").status_code == 422
        assert client.get("/api/history/health?range=bogus").status_code == 422

    def test_the_url_comes_from_victoria_metrics_url(self, client: TestClient, vm: FakeVictoriaMetrics, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VICTORIA_METRICS_URL", "http://vm.internal:9999/")
        client.get("/api/history/queues")
        assert {(r.url.host, r.url.port) for r in vm.requests} == {("vm.internal", 9999)}

    def test_series_are_merged_per_queue_and_dlqs_are_split_out(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        vm.answers = [
            (
                "/api/v1/query_range",
                "rabbitmq_queue_messages_ready",
                [_series("queue", QUEUE, [(1000, "5"), (1300, "7")]), _series("queue", DLQ, [(1000, "2")])],
            ),
            ("/api/v1/query_range", "rabbitmq_queue_messages_unacked", [_series("queue", QUEUE, [(1000, "1"), (1300, "0")])]),
            ("/api/v1/query_range", "rabbitmq_queue_consumers", [_series("queue", QUEUE, [(1000, "2"), (1300, "2")])]),
            ("/api/v1/query_range", "messages_published_total", [_series("queue", QUEUE, [(1000, "NaN"), (1300, "1.5")])]),
        ]
        body = client.get("/api/history/queues").json()
        assert [q["queue_name"] for q in body["queues"]] == [QUEUE]
        assert [q["queue_name"] for q in body["dlq_summary"]] == [DLQ]
        first, second = body["queues"][0]["data_points"]
        assert first["timestamp"] == "1970-01-01T00:16:40+00:00"
        assert (first["messages_ready"], first["messages_unacknowledged"], first["consumers"]) == (5.0, 1.0, 2.0)
        assert first["publish_rate"] is None  # NaN is no data, not zero
        assert second["publish_rate"] == 1.5
        assert second["ack_rate"] is None  # no ack series was scraped
        assert body["dlq_summary"][0]["data_points"][0]["messages_ready"] == 2.0


class TestHealthRoute:
    @pytest.mark.parametrize("range_name", list(STEPS))
    def test_each_range_uses_its_step_and_window(self, client: TestClient, vm: FakeVictoriaMetrics, range_name: str) -> None:
        assert client.get(f"/api/history/health?range={range_name}").status_code == 200
        step, window = STEPS[range_name]
        ranged = [(r, dict(r.url.params)) for r in vm.requests if r.url.path == "/api/v1/query_range"]
        instant = [dict(r.url.params) for r in vm.requests if r.url.path == "/api/v1/query"]
        assert len(ranged) == 5 and len(instant) == 5
        assert all(p["step"] == step and f"[{window}]" in p["query"] for _, p in ranged)
        assert all(f"[{range_name}]" in p["query"] and "step" not in p for p in instant)

    def test_services_response_times_and_endpoints_are_shaped(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        vm.answers = [
            (
                "/api/v1/query_range",
                'outcome="success"}[5m])) / sum by (target) (increase',
                [_series("target", "api", [(1000, "1"), (1300, "1")]), _series("target", "neo4j", [(1000, "1"), (1300, "0.5")])],
            ),
            ("/api/v1/query_range", "_sum{", [_series("target", "api", [(1000, "10"), (1300, "30")])]),
            ("/api/v1/query_range", "histogram_quantile(0.95", [{"metric": {}, "values": [[1000, "12.5"], [1300, "NaN"]]}]),
            (
                "/api/v1/query",
                "topk(",
                [
                    {"metric": {"service_name": "api", "http_route": "/a"}, "value": [1300, "10"]},
                    {"metric": {"service_name": "api", "http_route": "/b"}, "value": [1300, "200"]},
                ],
            ),
            ("/api/v1/query", "http_response_status_code", [{"metric": {"service_name": "api", "http_route": "/a"}, "value": [1300, "1"]}]),
            ("/api/v1/query", "histogram_quantile(0.99", [{"metric": {"service_name": "api", "http_route": "/b"}, "value": [1300, "80"]}]),
        ]
        body = client.get("/api/history/health").json()
        services = {s["service_name"]: s for s in body["services"]}
        assert services["api"]["status"] == "healthy" and services["api"]["uptime_pct"] == 100.0
        assert services["api"]["response_time_ms"] == pytest.approx(20.0)  # the query already converts to ms
        assert services["neo4j"]["status"] == "degraded" and services["neo4j"]["uptime_pct"] == 75.0
        assert services["neo4j"]["response_time_ms"] is None
        assert body["response_time_series"][0]["p95"] == 12.5 and body["response_time_series"][1]["p95"] is None
        assert [e["endpoint"] for e in body["endpoints"]] == ["/b", "/a"]
        assert body["endpoints"][1]["error_pct"] == 10.0 and body["endpoints"][0]["p99"] == 80.0


class TestUnavailable:
    @pytest.mark.parametrize("path", ["/api/history/queues", "/api/history/health"])
    def test_a_non_200_answer_is_history_unavailable(self, client: TestClient, vm: FakeVictoriaMetrics, path: str) -> None:
        vm.status = 502
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["detail"] == "history unavailable"
        assert "victoria-metrics" not in response.text

    @pytest.mark.parametrize("path", ["/api/history/queues", "/api/history/health"])
    def test_an_unreachable_victoria_metrics_is_history_unavailable(self, client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        monkeypatch.setattr(history, "_transport", httpx.MockTransport(refuse))
        assert client.get(path).status_code == 503

    def test_the_live_overview_keeps_working(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(history, "_transport", httpx.MockTransport(lambda r: httpx.Response(500)))
        assert client.get("/api/history/queues").status_code == 503
        assert client.get("/api/queues").status_code == 200
        assert client.get("/api/services").status_code == 200

    def test_a_failed_query_status_is_history_unavailable(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        body = json.dumps({"status": "error", "error": "bad"})
        monkeypatch.setattr(
            history, "_transport", httpx.MockTransport(lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
        )
        assert client.get("/api/history/health").status_code == 503


def test_the_history_routes_require_a_session(fake_catalog: FakeCatalogApi) -> None:
    with TestClient(create_test_app()) as anonymous:
        assert anonymous.get("/api/history/queues").status_code == 401
        assert anonymous.get("/api/history/health").status_code == 401
