"""Generate the spike's SYNTHETIC extraction fixtures.

No real extraction could be recorded for this spike (see the spike document's Method), so this
script simulates one. It models the broker counters a full Discogs and MusicBrainz run would move,
renders every 10-second poll inside the sparkline window as a RabbitMQ 4.4 ``/metrics/detailed``
body in the shape of ``tests/fixtures/rabbitmq_4_4_metrics_detailed.txt``, and reads those bodies
back through the console's own ``parse_queue_samples`` and ``RateTracker``. The queue values the
prototypes draw are therefore exactly what the live poller would compute from such scrapes; only
the counters underneath are invented.

Run from the repository root:

    uv run python docs/spikes/gm-operations-console-2rf/simulate.py
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from dashboard.queue_metrics import QueueSample, RateTracker, parse_queue_samples


HERE = Path(__file__).resolve().parent
STEP = 10  # seconds between polls (the console polls every 2 s; 10 s keeps the fixture small)
WINDOW = 900  # sparkline window, seconds
HISTORY_STEP = 300  # run burn-up sampling, seconds
PREFETCH = 200  # consumers prefetch 2 x POSTGRES_BATCH_SIZE (100), so a busy queue holds ~200 unacked
NOW_ISO = "2026-10-23T14:00:00Z"


@dataclass(frozen=True)
class Entity:
    name: str
    total: int  # records in the dump file (approximate public dump sizes)
    publish_rate: float  # extractor records/s while the file is being read
    start: int = 0  # seconds after run start the extractor opens the file


@dataclass(frozen=True)
class Consumer:
    name: str
    store: str
    capacity: dict[str, float]  # max ack rate per entity, messages/s
    hypothetical: bool = False


@dataclass(frozen=True)
class Pipeline:
    name: str
    producer: str
    prefix: str
    run_elapsed: int  # seconds since the run started, at NOW
    entities: tuple[Entity, ...]
    consumers: tuple[Consumer, ...]


@dataclass(frozen=True)
class Fault:
    kind: str  # "stalled" | "dlq"
    pipeline: str
    consumer: str
    entity: str
    since: int  # seconds before NOW the fault began
    reject_fraction: float = 0.0


@dataclass
class QueueState:
    ready: float = 0.0
    unacked: float = 0.0
    published: float = 0.0
    acked: float = 0.0
    consumers: int = 1


@dataclass
class EntityState:
    extracted: float = 0.0


@dataclass
class Sim:
    pipeline: Pipeline
    faults: list[Fault]
    queues: dict[tuple[str, str], QueueState] = field(default_factory=dict)
    dlqs: dict[tuple[str, str], QueueState] = field(default_factory=dict)
    entities: dict[str, EntityState] = field(default_factory=dict)
    last_publish: dict[str, float] = field(default_factory=dict)


def wobble(t: float, seed: float) -> float:
    """Deterministic +-6 % rate noise, so sparklines are not ruler-straight."""
    return 1.0 + 0.04 * math.sin(t / 47.0 + seed) + 0.02 * math.sin(t / 11.0 + 2.3 * seed)


DISCOGS = Pipeline(
    name="discogs",
    producer="extractor-discogs",
    prefix="groovemap-discogs",
    run_elapsed=9600,
    entities=(
        Entity("masters", 2_400_000, 1500.0),
        Entity("releases", 17_000_000, 1050.0),
        Entity("artists", 9_300_000, 1250.0),
        Entity("labels", 2_700_000, 1800.0),
    ),
    consumers=(
        Consumer("graphinator", "neo4j", {"masters": 1200, "releases": 900, "artists": 1100, "labels": 1500}),
        Consumer("tableinator", "postgresql", {"masters": 2500, "releases": 1400, "artists": 2000, "labels": 2500}),
    ),
)
MUSICBRAINZ = Pipeline(
    name="musicbrainz",
    producer="extractor-musicbrainz",
    prefix="groovemap-musicbrainz",
    run_elapsed=6600,
    entities=(
        Entity("release-groups", 3_900_000, 1400.0),
        Entity("releases", 5_000_000, 700.0),
        Entity("artists", 2_600_000, 1100.0),
        Entity("labels", 300_000, 900.0),
    ),
    consumers=(
        Consumer("brainzgraphinator", "neo4j", {"release-groups": 1000, "releases": 520, "artists": 900, "labels": 800}),
        Consumer("brainztableinator", "postgresql", {"release-groups": 2000, "releases": 1100, "artists": 1800, "labels": 1500}),
    ),
)


def doubled(pipeline: Pipeline) -> Pipeline:
    """The same pipeline with two hypothetical extra consumers, for the 2x-consumers stress case."""
    base = "brainz" if pipeline.name == "musicbrainz" else ""
    capacity = {entity.name: entity.publish_rate * 1.4 for entity in pipeline.entities}
    extra = (
        Consumer(f"{base}searchinator", "neo4j", capacity, hypothetical=True),
        Consumer(f"{base}embedinator", "postgresql", capacity, hypothetical=True),
    )
    return Pipeline(**{**pipeline.__dict__, "consumers": (*pipeline.consumers, *extra)})


STALL = Fault("stalled", "discogs", "tableinator", "releases", since=420)
DLQ = Fault("dlq", "musicbrainz", "brainzgraphinator", "releases", since=720, reject_fraction=0.008)
# Every scenario, healthy included, carries a small, flat, months-old DLQ: a realistic distractor
# that a view must not confuse with a growing one.
STATIC_DLQ = {("discogs", "graphinator", "artists"): 12}

SCENARIOS = {
    "healthy": {
        "title": "Healthy run",
        "description": "Both runs extracting releases; graph consumers fill a backlog they drain after extraction.",
        "pipelines": (DISCOGS, MUSICBRAINZ),
        "faults": [],
    },
    "stalled-consumer": {
        "title": "Stalled consumer",
        "description": "tableinator stopped acknowledging discogs releases 7 minutes ago but stays connected.",
        "pipelines": (DISCOGS, MUSICBRAINZ),
        "faults": [STALL],
    },
    "growing-dlq": {
        "title": "Growing DLQ",
        "description": "brainzgraphinator rejects 0.8 % of musicbrainz releases to its DLQ, starting 12 minutes ago.",
        "pipelines": (DISCOGS, MUSICBRAINZ),
        "faults": [DLQ],
    },
    "stalled-consumer-2x": {
        "title": "Stalled consumer, 2x consumers",
        "description": "The stalled-consumer case with two hypothetical extra consumers per pipeline.",
        "pipelines": (doubled(DISCOGS), doubled(MUSICBRAINZ)),
        "faults": [STALL],
    },
}


def fault_for(sim: Sim, consumer: str, entity: str, kind: str) -> Fault | None:
    return next(
        (f for f in sim.faults if f.kind == kind and f.pipeline == sim.pipeline.name and f.consumer == consumer and f.entity == entity),
        None,
    )


def make_sim(pipeline: Pipeline, faults: list[Fault]) -> Sim:
    sim = Sim(pipeline, faults)
    for entity in pipeline.entities:
        sim.entities[entity.name] = EntityState()
        for consumer in pipeline.consumers:
            sim.queues[(consumer.name, entity.name)] = QueueState()
            dlq = QueueState(consumers=0)
            dlq.ready = dlq.published = STATIC_DLQ.get((pipeline.name, consumer.name, entity.name), 0)
            sim.dlqs[(consumer.name, entity.name)] = dlq
    return sim


def step(sim: Sim, t: int) -> None:
    """Advance the model by one poll interval ending at run time ``t``."""
    remaining = sim.pipeline.run_elapsed - t  # seconds before NOW
    for index, entity in enumerate(sim.pipeline.entities):
        state = sim.entities[entity.name]
        published = 0.0
        if t >= entity.start and state.extracted < entity.total:
            published = min(entity.publish_rate * wobble(t, index) * STEP, entity.total - state.extracted)
            state.extracted += published
        sim.last_publish[entity.name] = published / STEP
        for c_index, consumer in enumerate(sim.pipeline.consumers):
            queue = sim.queues[(consumer.name, entity.name)]
            queue.published += published
            queue.ready += published
            stall = fault_for(sim, consumer.name, entity.name, "stalled")
            dlq_fault = fault_for(sim, consumer.name, entity.name, "dlq")
            if stall is not None and remaining < stall.since:
                # Connected but hung: the prefetch window stays delivered, nothing is acknowledged.
                queue.ready = max(0.0, queue.ready + queue.unacked - PREFETCH)
                queue.unacked = PREFETCH
                continue
            backlog = queue.ready + queue.unacked
            # While the extractor publishes, a keeping-up consumer still holds a prefetch window in flight.
            in_flight = min(backlog, PREFETCH) if published else 0.0
            delivered = min(backlog - in_flight, consumer.capacity[entity.name] * wobble(t, 7 + index + 3 * c_index) * STEP)
            rejected = 0.0
            if dlq_fault is not None and remaining < dlq_fault.since:
                rejected = delivered * dlq_fault.reject_fraction
            queue.acked += delivered - rejected
            backlog -= delivered
            queue.unacked = min(backlog, PREFETCH)
            queue.ready = backlog - queue.unacked
            dlq = sim.dlqs[(consumer.name, entity.name)]
            dlq.published += rejected
            dlq.ready += rejected


def exposition(sim: Sim) -> str:
    """Render the current model state as a /metrics/detailed body for this pipeline's prefix."""
    gauges: dict[str, list[str]] = {
        "rabbitmq_detailed_queue_messages_ready": [],
        "rabbitmq_detailed_queue_messages_unacked": [],
        "rabbitmq_detailed_queue_messages": [],
        "rabbitmq_detailed_queue_consumers": [],
        "rabbitmq_detailed_queue_messages_acked_total": [],
        "rabbitmq_detailed_queue_exchange_messages_published_total": [],
    }
    types = {name: ("counter" if name.endswith("_total") else "gauge") for name in gauges}
    for (consumer, entity), queue in sim.queues.items():
        for name, state, exchange in (
            (f"{sim.pipeline.prefix}-{consumer}-{entity}", queue, f"{sim.pipeline.prefix}-{entity}"),
            (f"{sim.pipeline.prefix}-{consumer}-{entity}.dlq", sim.dlqs[(consumer, entity)], f"{sim.pipeline.prefix}-{consumer}-{entity}.dlx"),
        ):
            labels = f'vhost="/",queue="{name}"'
            ready, unacked = round(state.ready), round(state.unacked)
            gauges["rabbitmq_detailed_queue_messages_ready"].append(f"{{{labels}}} {ready}")
            gauges["rabbitmq_detailed_queue_messages_unacked"].append(f"{{{labels}}} {unacked}")
            gauges["rabbitmq_detailed_queue_messages"].append(f"{{{labels}}} {ready + unacked}")
            gauges["rabbitmq_detailed_queue_consumers"].append(f"{{{labels}}} {state.consumers}")
            # A counter series appears only after its first event, as on the real broker.
            if state.acked:
                gauges["rabbitmq_detailed_queue_messages_acked_total"].append(f"{{{labels}}} {round(state.acked)}")
            if state.published:
                gauges["rabbitmq_detailed_queue_exchange_messages_published_total"].append(
                    f'{{vhost="/",exchange="{exchange}",queue="{name}"}} {round(state.published)}'
                )
    lines: list[str] = []
    for name, samples in gauges.items():
        lines.append(f"# TYPE {name} {types[name]}")
        lines.extend(f"{name}{sample}" for sample in samples)
    return "\n".join(lines) + "\n"


def run_scenario(pipeline: Pipeline, faults: list[Fault]) -> tuple[dict, str]:
    sim = make_sim(pipeline, faults)
    tracker = RateTracker()
    window_start = pipeline.run_elapsed - WINDOW
    series: dict[str, dict[str, list[float]]] = {}
    last: dict[str, dict[str, float]] = {}
    burnup: dict[str, list[float]] = {f"{c.name}|{e.name}": [] for c in pipeline.consumers for e in pipeline.entities}
    extracted_history: dict[str, list[float]] = {e.name: [] for e in pipeline.entities}
    history_t: list[int] = []
    body = ""
    for t in range(STEP, pipeline.run_elapsed + 1, STEP):
        step(sim, t)
        if t % HISTORY_STEP == 0 or t == pipeline.run_elapsed:
            history_t.append(t)
            for entity in pipeline.entities:
                extracted_history[entity.name].append(round(sim.entities[entity.name].extracted))
                for consumer in pipeline.consumers:
                    burnup[f"{consumer.name}|{entity.name}"].append(round(sim.queues[(consumer.name, entity.name)].acked))
        if t < window_start - STEP:
            continue
        body = exposition(sim)
        samples: dict[tuple[str, str], QueueSample] = parse_queue_samples(body)
        rates = tracker.update(samples, float(t))
        if t < window_start:
            continue  # baseline scrape only
        for (_vhost, name), sample in sorted(samples.items(), key=lambda item: item[0][1]):
            publish, ack = rates[("/", name)]
            point = {
                "messages": sample.messages,
                "messages_ready": sample.messages_ready,
                "messages_unacknowledged": sample.messages_unacknowledged,
                "consumers": sample.consumers,
                "message_rate": round(publish, 1),
                "ack_rate": round(ack, 1),
            }
            last[name] = point
            entry = series.setdefault(name, {"messages": [], "message_rate": [], "ack_rate": []})
            entry["messages"].append(point["messages"])
            entry["message_rate"].append(point["message_rate"])
            entry["ack_rate"].append(point["ack_rate"])

    queues = []
    for consumer in pipeline.consumers:
        for entity in pipeline.entities:
            for dlq in (False, True):
                name = f"{pipeline.prefix}-{consumer.name}-{entity.name}" + (".dlq" if dlq else "")
                state = sim.dlqs[(consumer.name, entity.name)] if dlq else sim.queues[(consumer.name, entity.name)]
                queues.append(
                    {
                        "name": name,
                        "consumer": consumer.name,
                        "entity": entity.name,
                        "dlq": dlq,
                        "last": last[name],
                        "series": series[name],
                        "acked_since_run_start": round(state.acked),
                    }
                )
    extractor = {}
    for entity in pipeline.entities:
        state = sim.entities[entity.name]
        extractor[entity.name] = {
            "extracted": round(state.extracted),
            # groovemap.extraction.file.progress: bytes read / file size. Modelled as linear in records.
            "file_progress": round(state.extracted / entity.total, 4),
            "publish_rate": round(sim.last_publish[entity.name], 1),
            "done": state.extracted >= entity.total,
        }
    return (
        {
            "name": pipeline.name,
            "producer": pipeline.producer,
            "exchange_prefix": pipeline.prefix,
            "entities": [entity.name for entity in pipeline.entities],
            "consumers": [{"name": c.name, "store": c.store, "hypothetical": c.hypothetical} for c in pipeline.consumers],
            "run": {"elapsed_s": pipeline.run_elapsed, "history_t": history_t},
            "extractor": extractor,
            "extracted_history": extracted_history,
            "burnup": burnup,
            "queues": queues,
        },
        body,
    )


def main() -> None:
    fixtures: dict = {
        "synthetic": True,
        "note": "SYNTHETIC. Simulated counters read through dashboard.queue_metrics; not recorded from a real extraction.",
        "now": NOW_ISO,
        "step_s": STEP,
        "window_s": WINDOW,
        "scenarios": {},
    }
    (HERE / "fixtures").mkdir(exist_ok=True)
    for key, scenario in SCENARIOS.items():
        pipelines = []
        for pipeline in scenario["pipelines"]:
            data, body = run_scenario(pipeline, scenario["faults"])
            pipelines.append(data)
            if key == "stalled-consumer" and pipeline.name == "discogs":
                (HERE / "fixtures/stalled-consumer-discogs-last-scrape.txt").write_text(body)
        fixtures["scenarios"][key] = {
            "title": scenario["title"],
            "description": scenario["description"],
            "faults": [
                {"kind": f.kind, "pipeline": f.pipeline, "consumer": f.consumer, "entity": f.entity, "since_s": f.since} for f in scenario["faults"]
            ],
            "pipelines": pipelines,
        }
    text = json.dumps(fixtures, separators=(",", ":"))
    (HERE / "fixtures.js").write_text(f"// Generated by simulate.py. SYNTHETIC data, not a recording.\nwindow.SPIKE_FIXTURES = {text};\n")


if __name__ == "__main__":
    main()
