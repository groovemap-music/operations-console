"""Run progress: what one extraction run has loaded, read from VictoriaMetrics.

Percent loaded and an ETA need two numbers the live snapshot does not have (spike
gm-operations-console-2rf). The first is how many records each entity will have: the extractor's
``groovemap_extraction_file_progress_ratio{source,entity}`` gauge is bytes read over file size, so
expected = extracted / ratio. Extracted is the run's increase of
``groovemap_extraction_records_total{source,entity}``, which the extractor exports in the same
batch as the gauge, so the two describe the same instant. The second is how many each queue has
acknowledged since the run started: the run's increase of
``rabbitmq_detailed_queue_messages_acked_total{queue}`` over its value at the start (the baseline).

Nothing is kept in the console. Every request reads the baselines from VictoriaMetrics at the
run's own start time, so a console restart mid-run changes nothing. The browser names the run
(pipeline, start, and end once it finished) from the extraction history it reads through the admin
proxy, and computes the ETA from its live samples with the matrix's own rules.

A number that cannot be derived is null, never a guess: no gauge sample in the run, no record yet,
or no baseline at the start. The route sits behind the session gate; VictoriaMetrics being
unreachable answers 503 like the other history routes.
"""

import asyncio
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from dashboard.history import HistoryUnavailableError, _iso, _number, _unavailable, query_instant, query_span, vm_client


router = APIRouter()

ACKED = "rabbitmq_detailed_queue_messages_acked_total"
RECORDS = "groovemap_extraction_records_total"
FILE_PROGRESS = "groovemap_extraction_file_progress_ratio"

# The burn-up is drawn from about this many points, never closer than the 15 s scrape interval.
BURNUP_POINTS = 120
MIN_STEP_SECONDS = 15
MAX_RUN = timedelta(days=90)

# The pipelines as /api/topology describes them, keyed by id; set once by the app.
_pipelines: dict[str, dict[str, Any]] = {}


def configure(pipelines: list[dict[str, Any]]) -> None:
    """Name the pipelines the route answers for, in the shape ``build_topology`` returns."""
    _pipelines.clear()
    _pipelines.update({pipeline["id"]: pipeline for pipeline in pipelines})


def progress_queries(source: str, queue_prefix: str, span_seconds: int) -> dict[str, str]:
    """PromQL for one pipeline's run: acked per queue, records per entity, and file progress in the run."""
    prefix = re.escape(queue_prefix).replace("\\", "\\\\")
    return {
        "acked": f'sum by (queue) ({ACKED}{{queue=~"{prefix}-.*"}})',
        "records": f'sum by (entity) ({RECORDS}{{source="{source}"}})',
        # Only a sample inside the run counts: an older one describes the previous run's file.
        "file_progress": f'max by (entity) (last_over_time({FILE_PROGRESS}{{source="{source}"}}[{span_seconds}s]))',
    }


def step_seconds(span_seconds: float) -> int:
    """The burn-up step for a run of ``span_seconds``."""
    return max(MIN_STEP_SECONDS, math.ceil(span_seconds / BURNUP_POINTS))


def instant_values(result: list[dict[str, Any]], label: str) -> dict[str, float]:
    """``{label value: sample}`` from an instant query, dropping NaN and missing labels."""
    values: dict[str, float] = {}
    for series in result:
        key = series.get("metric", {}).get(label)
        value = _number(series.get("value", [0, None])[1])
        if key is not None and value is not None:
            values[key] = value
    return values


def range_values(result: list[dict[str, Any]], label: str) -> dict[str, list[tuple[float, float]]]:
    """``{label value: [(timestamp, sample), ...]}`` from a range query, oldest first."""
    values: dict[str, list[tuple[float, float]]] = {}
    for series in result:
        key = series.get("metric", {}).get(label)
        if key is None:
            continue
        points = [(float(ts), value) for ts, raw in series.get("values", []) if (value := _number(raw)) is not None]
        values[key] = sorted(points)
    return values


def counter_increase(baseline: float | None, points: list[tuple[float, float]], start: float) -> list[tuple[float, float]] | None:
    """The counter's cumulative increase since ``baseline`` at each point after ``start``.

    A sample below the one before it is a counter reset (a broker or extractor restart): the
    counter started again from zero, so its whole value is new. None without a baseline.
    """
    if baseline is None:
        return None
    previous, total, increases = baseline, 0.0, []
    for ts, value in points:
        if ts <= start:
            continue
        total += value - previous if value >= previous else value
        previous = value
        increases.append((ts, total))
    return increases


def expected_records(extracted: float | None, file_progress: float | None) -> int | None:
    """Records the entity will have: extracted / bytes read over file size; None until both are known."""
    if extracted is None or file_progress is None or extracted <= 0 or file_progress <= 0:
        return None
    return round(extracted / min(file_progress, 1.0))


def _with_end(points: list[tuple[float, float]], end: float, value: float | None) -> list[tuple[float, float]]:
    """The range samples, closed by the instant sample at the run's end (the range stops a step short)."""
    if value is None or (points and points[-1][0] >= end):
        return points
    return [*points, (end, value)]


def shape_progress(pipeline: dict[str, Any], start: float, end: float, results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The run's per-entity totals and per-queue loaded counts, with the burn-up points."""
    acked_start = instant_values(results["acked_start"], "queue")
    acked_end = instant_values(results["acked_end"], "queue")
    acked_points = range_values(results["acked_range"], "queue")
    records_start = instant_values(results["records_start"], "entity")
    records_end = instant_values(results["records_end"], "entity")
    records_points = range_values(results["records_range"], "entity")
    file_progress = instant_values(results["file_progress"], "entity")
    # The extractor creates a records series with its first batch, so one missing at the start had
    # counted nothing yet. That holds only if VictoriaMetrics held data then at all, which the
    # acked baselines (long-lived queues) show.
    held_data_at_start = bool(acked_start)

    entities = []
    for entity in pipeline["entity_types"]:
        baseline = records_start.get(entity, 0.0) if held_data_at_start else None
        increase = counter_increase(baseline, _with_end(records_points.get(entity, []), end, records_end.get(entity)), start)
        extracted = round(increase[-1][1]) if increase else (0 if increase is not None else None)
        ratio = file_progress.get(entity)
        entities.append({"entity": entity, "extracted": extracted, "file_progress": ratio, "expected": expected_records(extracted, ratio)})

    queues = []
    for consumer in pipeline["consumers"]:
        for queue in consumer["queues"]:
            name = queue["name"]
            baseline = acked_start.get(name)
            increase = counter_increase(baseline, _with_end(acked_points.get(name, []), end, acked_end.get(name)), start)
            loaded = None if increase is None else (round(increase[-1][1]) if increase else 0)
            queues.append(
                {
                    "queue": name,
                    "consumer": consumer["name"],
                    "entity": queue["entity"],
                    "baseline": baseline,
                    "loaded": loaded,
                    "points": [[_iso(ts), round(value)] for ts, value in increase or []],
                }
            )
    return {"pipeline": pipeline["id"], "start": _iso(start), "end": _iso(end), "entities": entities, "queues": queues}


async def read_progress(pipeline: dict[str, Any], start: float, end: float) -> dict[str, Any]:
    """Query VictoriaMetrics for one run and shape the answer; raises HistoryUnavailableError."""
    queries = progress_queries(pipeline["id"], pipeline["queue_prefix"], max(1, math.ceil(end - start)))
    step = f"{step_seconds(end - start)}s"
    async with vm_client() as client:
        names = ["acked_start", "acked_end", "acked_range", "records_start", "records_end", "records_range", "file_progress"]
        values = await asyncio.gather(
            query_instant(client, queries["acked"], start),
            query_instant(client, queries["acked"], end),
            query_span(client, queries["acked"], start, end, step),
            query_instant(client, queries["records"], start),
            query_instant(client, queries["records"], end),
            query_span(client, queries["records"], start, end, step),
            query_instant(client, queries["file_progress"], end),
        )
    return shape_progress(pipeline, start, end, dict(zip(names, values, strict=True)))


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


@router.get("/api/progress/{pipeline_id}")
async def run_progress(pipeline_id: str, start: datetime = Query(), end: datetime | None = Query(default=None)) -> JSONResponse:
    """What the run of ``pipeline_id`` that started at ``start`` had loaded by ``end`` (default: now)."""
    pipeline = _pipelines.get(pipeline_id)
    if pipeline is None:
        return _error(404, "unknown pipeline")
    now = datetime.now(UTC)
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    end = min(end if end and end.tzinfo else (end.replace(tzinfo=UTC) if end else now), now)
    if start >= end:
        return _error(400, "start must be before end and in the past")
    if end - start > MAX_RUN:
        return _error(400, "run is longer than the retained history")
    try:
        body = await read_progress(pipeline, start.timestamp(), end.timestamp())
    except HistoryUnavailableError as exc:
        return _unavailable(str(exc))
    return JSONResponse(content=body)
