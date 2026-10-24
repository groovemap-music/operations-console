"""Queue Trends and System Health history, read from VictoriaMetrics.

History and the live view share one source of truth with Grafana: the console no longer asks
catalog-api for its PostgreSQL metrics samples, it runs PromQL against VictoriaMetrics'
``/api/v1/query_range`` (and ``/api/v1/query`` for the endpoint table, which is a snapshot over
the selected range). One time range drives every query; each range carries its own step and
rate window, pinned by the tests.

The routes are mounted behind the console's session gate. VictoriaMetrics being unreachable
answers 503 with a ``history unavailable`` detail; nothing here is on the live Overview's path.
"""

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from os import getenv
from typing import Any, Literal

import httpx
import structlog
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from dashboard.catalog_contract import DISCOGS_EXCHANGE_PREFIX, MUSICBRAINZ_EXCHANGE_PREFIX


logger = structlog.get_logger(__name__)

router = APIRouter()

DEFAULT_VICTORIA_METRICS_URL = "http://victoria-metrics:8428"
_TIMEOUT_SECONDS = 15.0
_ENDPOINT_LIMIT = 50

# Test seam: tests install a mock transport here instead of patching httpx globally, which the
# session gate's own client would also see.
_transport: httpx.AsyncBaseTransport | None = None

RangeName = Literal["1h", "6h", "24h", "7d", "30d", "90d"]


@dataclass(frozen=True)
class RangeSpec:
    """One selectable time range: how far back, the query step, and the rate window."""

    seconds: int
    step: str
    window: str


RANGES: dict[str, RangeSpec] = {
    "1h": RangeSpec(3600, "30s", "1m"),
    "6h": RangeSpec(6 * 3600, "2m", "2m"),
    "24h": RangeSpec(24 * 3600, "5m", "5m"),
    "7d": RangeSpec(7 * 86400, "30m", "30m"),
    "30d": RangeSpec(30 * 86400, "2h", "2h"),
    "90d": RangeSpec(90 * 86400, "6h", "6h"),
}

_POLL = "groovemap_console_poll_duration_seconds"
_HTTP = "http_server_request_duration_seconds"
# The poll histogram's "loop" target times the whole collection cycle, not a service.
_POLL_SELECTOR = 'target!="loop"'
_PERCENTILES = (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))


def victoria_metrics_url() -> str:
    """Base URL of VictoriaMetrics, read per request so a reconfigured deployment needs no reload."""
    return getenv("VICTORIA_METRICS_URL", DEFAULT_VICTORIA_METRICS_URL).rstrip("/")


def vm_client() -> httpx.AsyncClient:
    """A VictoriaMetrics client; tests swap its transport through ``_transport``."""
    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=_transport)


def _queue_matcher() -> str:
    """Label matcher restricting ``queue`` to the contract prefixes (PromQL regexes are fully anchored)."""
    alternation = "|".join(re.escape(p) for p in (DISCOGS_EXCHANGE_PREFIX, MUSICBRAINZ_EXCHANGE_PREFIX))
    return 'queue=~"(' + alternation.replace("\\", "\\\\") + ').*"'


def queue_queries(window: str) -> dict[str, str]:
    """PromQL per queue-history field, keyed by the field name served to the browser."""
    m = _queue_matcher()
    return {
        "messages_ready": f"sum by (queue) (rabbitmq_queue_messages_ready{{{m}}})",
        "messages_unacknowledged": f"sum by (queue) (rabbitmq_queue_messages_unacked{{{m}}})",
        "messages_total": f"sum by (queue) (rabbitmq_queue_messages{{{m}}})",
        "consumers": f"sum by (queue) (rabbitmq_queue_consumers{{{m}}})",
        "publish_rate": f"sum by (queue) (rate(rabbitmq_detailed_queue_exchange_messages_published_total{{{m}}}[{window}]))",
        "ack_rate": f"sum by (queue) (rate(rabbitmq_detailed_queue_messages_acked_total{{{m}}}[{window}]))",
    }


def health_queries(window: str) -> dict[str, str]:
    """PromQL for the System Health range queries."""
    queries = {
        "availability": (
            f'sum by (target) (increase({_POLL}_count{{{_POLL_SELECTOR},outcome="success"}}[{window}]))'
            f" / sum by (target) (increase({_POLL}_count{{{_POLL_SELECTOR}}}[{window}]))"
        ),
        "response_time_ms": (
            f'1000 * sum by (target) (rate({_POLL}_sum{{{_POLL_SELECTOR},outcome="success"}}[{window}]))'
            f' / sum by (target) (rate({_POLL}_count{{{_POLL_SELECTOR},outcome="success"}}[{window}]))'
        ),
    }
    for label, quantile in _PERCENTILES:
        queries[label] = f'1000 * histogram_quantile({quantile}, sum by (le) (rate({_POLL}_bucket{{{_POLL_SELECTOR},outcome="success"}}[{window}])))'
    return queries


def endpoint_queries(range_name: str) -> dict[str, str]:
    """PromQL for the endpoint table, evaluated once over the whole selected range."""
    by_route = "service_name, http_route"
    queries = {
        "request_count": f"topk({_ENDPOINT_LIMIT}, sum by ({by_route}) (increase({_HTTP}_count[{range_name}])))",
        "error_count": f'sum by ({by_route}) (increase({_HTTP}_count{{http_response_status_code=~"5.."}}[{range_name}]))',
    }
    for label, quantile in _PERCENTILES:
        queries[label] = f"1000 * histogram_quantile({quantile}, sum by (le, {by_route}) (increase({_HTTP}_bucket[{range_name}])))"
    return queries


class HistoryUnavailableError(Exception):
    """VictoriaMetrics did not answer usefully."""


async def _fetch(client: httpx.AsyncClient, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
    try:
        response = await client.get(f"{victoria_metrics_url()}{path}", params=params)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HistoryUnavailableError(type(exc).__name__) from exc
    if not isinstance(body, dict) or body.get("status") != "success":
        raise HistoryUnavailableError("query failed")
    return list(body.get("data", {}).get("result", []))


async def query_range(client: httpx.AsyncClient, query: str, spec: RangeSpec, now: float) -> list[dict[str, Any]]:
    """Run one ``/api/v1/query_range`` over the spec's span at its step."""
    return await query_span(client, query, now - spec.seconds, now, spec.step)


async def query_span(client: httpx.AsyncClient, query: str, start: float, end: float, step: str) -> list[dict[str, Any]]:
    """Run one ``/api/v1/query_range`` from ``start`` to ``end`` (epoch seconds) at ``step``."""
    params = {"query": query, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": step}
    return await _fetch(client, "/api/v1/query_range", params)


async def query_instant(client: httpx.AsyncClient, query: str, now: float) -> list[dict[str, Any]]:
    """Run one ``/api/v1/query`` evaluated at ``now``."""
    return await _fetch(client, "/api/v1/query", {"query": query, "time": f"{now:.0f}"})


def _number(raw: Any) -> float | None:
    """A sample value, or None for the NaN/Inf VictoriaMetrics emits where a ratio has no data."""
    try:
        value = float(raw)
    except TypeError, ValueError:
        return None
    return value if math.isfinite(value) else None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _merge(results: dict[str, list[dict[str, Any]]], label: str) -> dict[str, dict[float, dict[str, float | None]]]:
    """Fold per-field matrix results into ``{label value: {timestamp: {field: value}}}``."""
    merged: dict[str, dict[float, dict[str, float | None]]] = {}
    for field, series_list in results.items():
        for series in series_list:
            key = series.get("metric", {}).get(label)
            if key is None:
                continue
            points = merged.setdefault(key, {})
            for ts, raw in series.get("values", []):
                points.setdefault(float(ts), {})[field] = _number(raw)
    return merged


def shape_queues(results: dict[str, list[dict[str, Any]]], fields: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-queue data points; dead-letter queues are split out the way the Queue Trends tab renders them."""
    queues: list[dict[str, Any]] = []
    dlqs: list[dict[str, Any]] = []
    merged = _merge(results, "queue")
    for name in sorted(merged):
        points = [{"timestamp": _iso(ts), **{f: merged[name][ts].get(f) for f in fields}} for ts in sorted(merged[name])]
        (dlqs if name.endswith(".dlq") else queues).append({"queue_name": name, "data_points": points})
    return queues, dlqs


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _status(availability: float | None) -> str:
    if availability is None:
        return "unknown"
    if availability >= 0.99:
        return "healthy"
    return "degraded" if availability > 0 else "unhealthy"


def shape_services(results: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """One card per poll target: current status, uptime over the range, mean response time."""
    merged = _merge({k: results[k] for k in ("availability", "response_time_ms")}, "target")
    services = []
    for target in sorted(merged):
        samples = [merged[target][ts] for ts in sorted(merged[target])]
        availability = [s.get("availability") for s in samples]
        latest = next((v for v in reversed(availability) if v is not None), None)
        uptime = _mean(availability)
        services.append(
            {
                "service_name": target,
                "status": _status(latest),
                "uptime_pct": None if uptime is None else uptime * 100,
                "response_time_ms": _mean([s.get("response_time_ms") for s in samples]),
            }
        )
    return services


def shape_response_series(results: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The overall p50/p95/p99 poll duration series, in milliseconds."""
    points: dict[float, dict[str, float | None]] = {}
    for label, _quantile in _PERCENTILES:
        for series in results[label]:
            for ts, raw in series.get("values", []):
                points.setdefault(float(ts), {})[label] = _number(raw)
    return [{"timestamp": _iso(ts), **{label: points[ts].get(label) for label, _ in _PERCENTILES}} for ts in sorted(points)]


def shape_endpoints(results: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The endpoint table rows, keyed by service and route, busiest first."""
    rows: dict[tuple[str, str], dict[str, float | None]] = {}
    for field, series_list in results.items():
        for series in series_list:
            metric = series.get("metric", {})
            key = (metric.get("service_name", ""), metric.get("http_route", ""))
            rows.setdefault(key, {})[field] = _number(series.get("value", [0, None])[1])
    endpoints = []
    for (service, route), fields in rows.items():
        count = fields.get("request_count")
        if not count:
            continue
        errors = fields.get("error_count") or 0.0
        endpoints.append(
            {
                "service_name": service,
                "endpoint": route,
                "request_count": round(count),
                "p50": fields.get("p50"),
                "p95": fields.get("p95"),
                "p99": fields.get("p99"),
                "error_pct": 100 * errors / count,
            }
        )
    return sorted(endpoints, key=lambda e: e["request_count"], reverse=True)


def _unavailable(reason: str) -> JSONResponse:
    logger.warning("history_unavailable", reason=reason)
    return JSONResponse(status_code=503, content={"detail": "history unavailable", "reason": reason})


async def _gather(client: httpx.AsyncClient, queries: dict[str, str], run: Any) -> dict[str, list[dict[str, Any]]]:
    names = list(queries)
    values = await asyncio.gather(*(run(client, queries[name]) for name in names))
    return dict(zip(names, values, strict=True))


@router.get("/api/history/queues")
async def queue_history(range: RangeName = Query(default="24h")) -> JSONResponse:
    """Per-queue depth, ready, unacked, consumers, and publish/ack rates over ``range``."""
    spec = RANGES[range]
    queries = queue_queries(spec.window)
    now = datetime.now(UTC).timestamp()
    try:
        async with vm_client() as client:
            results = await _gather(client, queries, lambda c, q: query_range(c, q, spec, now))
    except HistoryUnavailableError as exc:
        return _unavailable(str(exc))
    queues, dlqs = shape_queues(results, tuple(queries))
    return JSONResponse(content={"range": range, "step": spec.step, "queues": queues, "dlq_summary": dlqs})


@router.get("/api/history/health")
async def health_history(range: RangeName = Query(default="24h")) -> JSONResponse:
    """Service availability and response time, poll-duration percentiles, and endpoint latency over ``range``."""
    spec = RANGES[range]
    queries = health_queries(spec.window)
    endpoints = endpoint_queries(range)
    now = datetime.now(UTC).timestamp()
    try:
        async with vm_client() as client:
            results, endpoint_results = await asyncio.gather(
                _gather(client, queries, lambda c, q: query_range(c, q, spec, now)),
                _gather(client, endpoints, lambda c, q: query_instant(c, q, now)),
            )
    except HistoryUnavailableError as exc:
        return _unavailable(str(exc))
    return JSONResponse(
        content={
            "range": range,
            "step": spec.step,
            "services": shape_services(results),
            "response_time_series": shape_response_series(results),
            "endpoints": shape_endpoints(endpoint_results),
        }
    )
