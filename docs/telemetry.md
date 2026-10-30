# Operations-console telemetry

This page is for operators and developers who wire the console into an OpenTelemetry collector or
build dashboards and alerts on its signals. It lists the variables that control export and every
metric and span the console emits itself.

## Export settings

The console exports OpenTelemetry metrics and traces through `groovemap-runtime`'s `common.telemetry` bootstrap; there is no Prometheus `/metrics` scrape endpoint. Both signals push over OTLP/HTTP-protobuf to the same collector and are configured entirely through the standard OpenTelemetry environment variables:

- `OTEL_EXPORTER_OTLP_ENDPOINT` — collector base URL, for example `http://otel-collector:4318`. When unset, telemetry stays a no-op and the service behaves exactly as it does without it.
- `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` — metrics-only endpoint override.
- `OTEL_METRICS_EXPORTER` — `otlp` (default) or `none` to force export off.
- `OTEL_METRIC_EXPORT_INTERVAL` — push interval in milliseconds.
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` — traces-only endpoint override.
- `OTEL_TRACES_EXPORTER` — `otlp` (default) or `none` to force tracing off while metrics keep exporting.
- `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` — sampler selection and ratio; the bootstrap defaults to `parentbased_traceidratio` at `1.0`, and production turns the ratio down.
- `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` — override the resource's `service.name` and add attributes such as `service.namespace` and `deployment.environment.name`.

HTTP server and client metrics (`http.server.request.duration`, `http.client.request.duration`) are emitted automatically once the `otel-http` extra is installed. The console additionally records `groovemap.console.websocket.connections` (an up-down counter of active WebSocket clients) and `groovemap.console.poll.duration` (a histogram, keyed by `target` — a polled service key, `rabbitmq`, `neo4j`, `postgres`, or `loop` for the 2-second collection loop itself — and `outcome`).

## Neo4j gauges

Neo4j Community serves no Prometheus endpoint, so the console — which already holds a driver and already polls every target — is the program's observer for the graph store. It registers five observable gauges, read through the same driver on the metric reader's schedule and entirely separate from the two-second health poll:

| Gauge | Attributes | Source |
| --- | --- | --- |
| `groovemap.neo4j.up` | none | 1 when the last refresh answered, 0 when it did not |
| `groovemap.neo4j.nodes` | `label` | one count-store `MATCH (n:Label) RETURN count(n)` per schema label |
| `groovemap.neo4j.relationships` | `type` | one count-store `MATCH ()-[r:TYPE]->() RETURN count(r)` per schema relationship type |
| `groovemap.neo4j.transactions.active` | none | `SHOW TRANSACTIONS`, including this reading's own |
| `groovemap.neo4j.store.size.bytes` | `store` | `CALL dbms.queryJmx('org.neo4j:instance=kernel#0,name=Store sizes')`, when the procedure answers |

The label and relationship-type sets are the closed sets `database-schema` defines and are pinned in this repository's tests, so a schema change fails here before it can quietly change the attribute set under a dashboard or an alert rule.

Every count is answered from Neo4j's count store, never by a scan, and the query shape that guarantees it is exact. Neo4j plans `NodeCountFromCountStore` only when `count()` is the sole thing its `RETURN` projects, so each branch wraps the aggregation in a `CALL () { ... }` scoped subquery and projects the label outside it. Naming the label alongside the count in one `RETURN` makes it a grouping key and silently demotes the plan to a label scan: profiled on Neo4j 5.26.30 community, 5,001 database hits against 5,000 nodes where the count store answers in 1. `TestCountStorePlansAgainstRealNeo4j` profiles the generated queries against a live server to prove the plan; it skips unless `GROOVEMAP_NEO4J_PROFILE_URI` points at one.

All five callbacks share one refresh per export interval. Each query is bounded to two seconds and the refresh as a whole to ten, so a slow or unreachable store can neither hold the export open nor raise into it. When a refresh fails the console reports `groovemap.neo4j.up` 0 and omits every other observation: a zero count would be indistinguishable from a genuinely empty graph. The store sizes are the one optional reading, omitted on their own when the JMX procedure is unavailable, which says nothing about the store's health.

## Runtime metrics

`setup_telemetry` installs the process view for free: `process.cpu.time`, `process.cpu.utilization`, `process.memory.usage`, `process.memory.virtual`, `process.thread.count`, `process.open_file_descriptor.count`, `process.context_switches`, and the CPython garbage-collection counter. No `system.*` host metric is collected; node-exporter owns the host. The console additionally starts the event-loop sampler in its FastAPI lifespan, which records `groovemap.runtime.event_loop.lag` (a histogram in seconds, no attributes) once a second. A slow poll cycle that is really loop starvation shows up there rather than in `groovemap.console.poll.duration`.

## Spans

The console opens exactly one span of its own, `console.poll {target}`, a trace root around each poll of one target, carrying `target` and `outcome` and — when the poll failed with an exception — status `ERROR` with `error.type`. `target` is a configured service key, `rabbitmq`, `neo4j`, or `postgres`; the reserved `loop` target of `groovemap.console.poll.duration` gets no span, because a span around the whole cycle would demote every real poll span from a root to a child. The outbound health request, the Neo4j session, and the PostgreSQL query the poll performs are child spans, and long-lived WebSocket connections produce no span per frame.

Every other span comes from the library: `SERVER` and `CLIENT` spans from the FastAPI and httpx instrumentors, route-templated, and `{db.operation.name} {db.system.name}` `CLIENT` spans from the resilient Neo4j and PostgreSQL wrappers. Span names and attributes are closed, low-cardinality sets, and a span never carries a statement, an id, a URL, or an exception message. Per-span call counts and durations are derived by the collector's `spanmetrics` connector, never emitted here.
