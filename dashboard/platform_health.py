"""Health of every long-running platform service, polled concurrently, and the one-shot jobs.

The ingest pipelines report their own health (``PIPELINE_CONFIGS``). Everything else the compose
stack runs, the applications, the datastores and broker, and the observability stack, is checked
here, each by the endpoint its own healthcheck or operator documentation names, reached over the
shared ``groovemap`` network. Each check ends in one of three states: ``healthy``, ``unhealthy``
(it answered, but not with a healthy answer), or ``unreachable`` (it did not answer in time).

Every check runs at once and is bounded by ``PROBE_TIMEOUT_SECONDS`` as a whole, so one slow
target costs the poll that long, never the sum of the targets.

One-shot jobs are not services and have no endpoint. A job is listed with its last run's outcome
only where a durable record exists that the console's own connection can read; otherwise it says
so. The console never gets the Docker socket, so container exit state is not available.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from os import getenv
from typing import Any, Final

import httpx
from pydantic import BaseModel

from dashboard import telemetry as console_telemetry


HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNREACHABLE = "unreachable"

GROUP_APPLICATIONS = "applications"
GROUP_DATA = "data"
GROUP_OBSERVABILITY = "observability"
GROUPS: Final = (GROUP_APPLICATIONS, GROUP_DATA, GROUP_OBSERVABILITY)

# Where a reading came from: a probe this module ran, or a datastore reading the console already
# takes for the Storage tab. The latter is listed so the Data group is complete, but its changes
# are reported once, as datastore events and attention items, not a second time as service ones.
SOURCE_PROBE = "probe"
SOURCE_DATASTORE = "datastore"

# The whole of one check, connect through answer. A target slower than this is unreachable.
PROBE_TIMEOUT_SECONDS: Final = 3.0

KIND_HTTP = "http"
KIND_VALKEY_PING = "valkey-ping"
KIND_VALKEY_EXPORTER = "valkey-exporter"


class PlatformService(BaseModel):
    """One long-running service's health."""

    name: str
    group: str
    status: str  # healthy, unhealthy, unreachable
    last_seen: datetime | None
    error: str | None
    source: str = SOURCE_PROBE


class JobStatus(BaseModel):
    """A one-shot job: its last run where a durable record can be read, else ``monitored`` is False."""

    name: str
    monitored: bool
    outcome: str | None = None  # "loaded" when a durable record of a run exists
    detail: str
    last_run: datetime | None = None
    dump_id: str | None = None


@dataclass(frozen=True)
class Probe:
    """One service check: what to ask, and how to read the answer."""

    name: str
    group: str
    target: str  # a URL for the http kinds, "host:port" for the Valkey ping
    kind: str = KIND_HTTP


def _env_url(name: str, default: str) -> str:
    return getenv(name, "").strip().rstrip("/") or default


def build_probes(rabbitmq_prometheus_url: str) -> list[Probe]:
    """The probes, in display order, from the deployed ports and the console's own settings.

    Every URL was verified against the service's compose healthcheck and a running container
    (see the bead comment). ``catalog-api`` health is on 8005, its API on 8004. ``postgres`` and
    ``neo4j`` are not probed here: the console already holds connections to both.
    """
    victoria_metrics = _env_url("VICTORIA_METRICS_URL", "http://victoria-metrics:8428")
    grafana = _env_url("GRAFANA_API_URL", "http://grafana:3000")
    return [
        Probe("catalog-api", GROUP_APPLICATIONS, "http://api:8005/health"),
        Probe("explore", GROUP_APPLICATIONS, "http://explore:8007/health"),
        Probe("insights", GROUP_APPLICATIONS, "http://insights:8009/health"),
        Probe("valkey", GROUP_DATA, getenv("VALKEY_HEALTH_ADDRESS", "valkey:6379"), KIND_VALKEY_PING),
        # The broker's Prometheus plugin, one small metric family: the console already scrapes it.
        Probe("rabbitmq", GROUP_DATA, f"{rabbitmq_prometheus_url}/metrics/detailed?family=node_coarse_metrics"),
        Probe("victoria-metrics", GROUP_OBSERVABILITY, f"{victoria_metrics}/health"),
        Probe("victoria-traces", GROUP_OBSERVABILITY, _env_url("VICTORIA_TRACES_URL", "http://victoria-traces:10428") + "/health"),
        Probe("otel-collector", GROUP_OBSERVABILITY, _env_url("OTEL_COLLECTOR_HEALTH_URL", "http://otel-collector:13133")),
        Probe("grafana", GROUP_OBSERVABILITY, f"{grafana}/api/health"),
        Probe("postgres-exporter", GROUP_OBSERVABILITY, "http://postgres-exporter:9187/"),
        Probe("valkey-exporter", GROUP_OBSERVABILITY, "http://valkey-exporter:9121/metrics", KIND_VALKEY_EXPORTER),
        Probe("cadvisor", GROUP_OBSERVABILITY, "http://cadvisor:8080/healthz"),
        Probe("node-exporter", GROUP_OBSERVABILITY, "http://node-exporter:9100/"),
    ]


_REDIS_UP = re.compile(r"^redis_up\s+(\S+)\s*$", re.MULTILINE)


def _reading(probe: Probe, status: str, error: str | None = None) -> PlatformService:
    seen = datetime.now(UTC) if status != UNREACHABLE else None
    return PlatformService(name=probe.name, group=probe.group, status=status, last_seen=seen, error=error)


async def _http_check(client: httpx.AsyncClient, probe: Probe) -> PlatformService:
    response = await client.get(probe.target)
    if response.status_code != 200:
        return _reading(probe, UNHEALTHY, f"HTTP {response.status_code}")
    if probe.kind == KIND_VALKEY_EXPORTER:
        # The exporter's landing page answers even while Valkey is down; redis_up is what it sees.
        match = _REDIS_UP.search(response.text)
        if match is None:
            return _reading(probe, UNHEALTHY, "redis_up not reported")
        if match.group(1) != "1":
            return _reading(probe, UNHEALTHY, "exporter cannot reach Valkey (redis_up 0)")
    return _reading(probe, HEALTHY)


async def _valkey_ping(probe: Probe) -> PlatformService:
    host, _, port = probe.target.rpartition(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        writer.write(b"PING\r\n")
        await writer.drain()
        reply = (await reader.readline()).strip()
    finally:
        writer.close()
    if reply == b"+PONG":
        return _reading(probe, HEALTHY)
    # The production Valkey requires a password, which the console is never given. An answer of
    # NOAUTH still proves the server is up and speaking RESP, which is all liveness asks.
    if reply.startswith(b"-NOAUTH"):
        return _reading(probe, HEALTHY)
    return _reading(probe, UNHEALTHY, f"unexpected reply to PING: {reply[:40].decode(errors='replace') or 'none'}")


async def check_probe(client: httpx.AsyncClient, probe: Probe, timeout: float = PROBE_TIMEOUT_SECONDS) -> PlatformService:
    """Run one check, bounded as a whole by ``timeout``; any failure to answer is ``unreachable``."""
    with console_telemetry.poll_span(probe.name) as poll:
        try:
            if probe.kind == KIND_VALKEY_PING:
                return await asyncio.wait_for(_valkey_ping(probe), timeout)
            return await asyncio.wait_for(_http_check(client, probe), timeout)
        except TimeoutError as error:
            poll.failed(error)
            return _reading(probe, UNREACHABLE, f"no answer within {timeout:g}s")
        except Exception as error:
            poll.failed(error)
            return _reading(probe, UNREACHABLE, str(error) or type(error).__name__)


async def poll_platform(probes: list[Probe], timeout: float = PROBE_TIMEOUT_SECONDS) -> list[PlatformService]:
    """Check every probe at once and return the readings in probe order."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        return list(await asyncio.gather(*(check_probe(client, probe, timeout) for probe in probes)))


def datastore_services(databases: list[Any]) -> list[PlatformService]:
    """The two datastores the console is connected to, as Data-group rows from the readings it already takes."""
    services = []
    for database in databases:
        healthy = database.status == "healthy"
        services.append(
            PlatformService(
                name={"postgresql": "postgres"}.get(database.name.lower(), database.name.lower()),
                group=GROUP_DATA,
                status=HEALTHY if healthy else UNHEALTHY,
                last_seen=datetime.now(UTC) if healthy else None,
                error=None if healthy else database.error,
                source=SOURCE_DATASTORE,
            )
        )
    return services


# One-shot jobs the stack runs. ``schema-init`` and ``rabbitmq-dlq-policy-init`` leave no record
# the console can read: their only trace is a container exit state, behind the Docker socket.
UNMONITORED_JOBS: Final = ("schema-init", "rabbitmq-dlq-policy-init")
NOT_MONITORED = "not monitored here"
EMBEDDINGS_JOB = "embeddings"

# How long a job reading is reused. The embeddings run is monthly; the query below is index-driven
# but there is no reason to repeat it every two seconds.
JOB_CACHE_SECONDS: Final = 60.0

# The newest load recorded in public.artist_embeddings: ``model_version`` is per dump and indexed,
# so walk its distinct values through that index (a loose index scan) and read one row of each.
# ``computed_at`` is set once per load, so any row of a version carries the load's time. A
# full-table max() would read every embedding row.
LAST_EMBEDDINGS_LOAD_SQL: Final = """
WITH RECURSIVE versions AS (
    (SELECT model_version FROM public.artist_embeddings ORDER BY model_version LIMIT 1)
    UNION ALL
    SELECT (SELECT model_version FROM public.artist_embeddings WHERE model_version > v.model_version ORDER BY model_version LIMIT 1)
    FROM versions v WHERE v.model_version IS NOT NULL
)
SELECT s.source_dump_id, s.source_dump_date, s.computed_at
FROM versions v
CROSS JOIN LATERAL (
    SELECT source_dump_id, source_dump_date, computed_at FROM public.artist_embeddings WHERE model_version = v.model_version LIMIT 1
) s
WHERE v.model_version IS NOT NULL
ORDER BY s.computed_at DESC
LIMIT 1
"""


def unmonitored_jobs() -> list[JobStatus]:
    """The jobs with no readable record."""
    return [JobStatus(name=name, monitored=False, detail=NOT_MONITORED) for name in UNMONITORED_JOBS]


def embeddings_job(row: tuple[str, date, datetime] | None) -> JobStatus:
    """The embeddings job from the newest load record, or from its absence.

    The record is written by the load itself, so it shows the last load that completed, not a run
    that failed or is still going.
    """
    if row is None:
        return JobStatus(name=EMBEDDINGS_JOB, monitored=True, detail="no embeddings load recorded yet")
    dump_id, dump_date, computed_at = row
    return JobStatus(
        name=EMBEDDINGS_JOB,
        monitored=True,
        outcome="loaded",
        detail=f"last load wrote dump {dump_id} ({dump_date.isoformat()})",
        last_run=computed_at,
        dump_id=dump_id,
    )


def embeddings_unreadable() -> JobStatus:
    """The embeddings record could not be read with the console's connection (no table, no grant, no database)."""
    return JobStatus(name=EMBEDDINGS_JOB, monitored=False, detail=NOT_MONITORED)
