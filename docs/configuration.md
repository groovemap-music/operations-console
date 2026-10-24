# Operations-console configuration

Deployment owns runtime composition and supplies configuration through environment variables
and secret files. The console reads credentials with the `<VARIABLE>_FILE` convention when a
matching file variable is present; the plain variable is the fallback.

## Required datastore configuration

| Variable | Meaning |
| --- | --- |
| `NEO4J_HOST` | A bare host, combined with `NEO4J_PORT` (default `7687`), or a complete Neo4j URI. |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | Neo4j credentials. Both also accept `_FILE` variants. |
| `POSTGRES_HOST` | PostgreSQL host, optionally with an embedded port. An embedded port takes precedence over `POSTGRES_PORT`. |
| `POSTGRES_PORT` | PostgreSQL port when `POSTGRES_HOST` does not include one; default `5432`. |
| `POSTGRES_USERNAME` / `POSTGRES_PASSWORD` | PostgreSQL credentials. Both also accept `_FILE` variants. |
| `POSTGRES_DATABASE` | PostgreSQL database inspected by the dashboard. |

`NEO4J_TLS_ENABLED` defaults to `false`. When enabled, certificate verification remains on
unless `NEO4J_TLS_VERIFY` is explicitly `false`, `0`, or `no`.

## Service and queue configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `RABBITMQ_PROMETHEUS_URL` | `http://rabbitmq:15692` | Base URL of the broker's `rabbitmq_prometheus` plugin. The console reads queue depth, consumers, and rates from `{url}/metrics/detailed`; the endpoint is unauthenticated on the internal network, so the console needs no RabbitMQ credential and opens no AMQP connection. |
| `VICTORIA_METRICS_URL` | `http://victoria-metrics:8428` | Base URL of VictoriaMetrics. The Queue Trends and System Health tabs query `{url}/api/v1/query_range` (and `/api/v1/query` for the endpoint table) through the console's own `/api/history/queues` and `/api/history/health` routes, for the ranges 1h, 6h, 24h, 7d, 30d and 90d; 90d needs the deployment's 90-day retention. The routes are behind the admin session like every other data route. When VictoriaMetrics does not answer they return 503 `history unavailable` and the tabs say so, while the live Overview keeps working. |
| `DISCOGS_EXCHANGE_PREFIX` | `groovemap-discogs` | Prefix promoted from the `discogs-ingestion` catalog-event contract. |
| `MUSICBRAINZ_EXCHANGE_PREFIX` | `groovemap-musicbrainz` | Prefix promoted from the `musicbrainz-ingestion` catalog-event contract. |
| `API_HOST` | `api` | Internal `catalog-api` host used by the constrained admin proxy and to validate admin sessions. |
| `API_PORT` | `8004` | Internal `catalog-api` port. |
| `SESSION_CACHE_SECONDS` | `30` | How long a confirmed admin session is trusted before the console asks `catalog-api` again. Must be a finite number: `nan`, `inf`, or a non-number fails startup. Clamped to 0–30; `0` validates every request. Only positive answers are cached, and never past the session's own expiry. |

Publish and ack rates are not broker-reported values: the Prometheus plugin exposes cumulative counters, and the console divides the counter delta between consecutive two-second polls by the elapsed time. A queue's first sample, the first sample after it was missing from a poll, an interval in which a counter went down (a reset), and an interval with no elapsed time have no computable rate and report `null` (`message_rate` and `ack_rate` in `/api/metrics`, the WebSocket payload, and `/api/queues`). `null` means "unknown this interval"; `0.0` is a computed zero (the counter did not move), and a counter series the broker has not emitted yet on an already-seen queue counts as zero. Consumers must treat `null` as unknown, never as idle: a stall rule needs a computed `0.0` ack rate sustained for its window. The browser renders `null` as an em dash labelled "rate unknown". If a scrape fails or returns a non-200 status, the console logs a warning and marks that pipeline's queue data unavailable (`queues_available: false` in `/api/metrics` and the WebSocket payload, `null` for the pipeline in `/api/queues`) instead of reporting an empty pipeline.

History is read from VictoriaMetrics, not catalog-api's PostgreSQL samples. Queue depth, ready, unacked and consumers come from the per-object `rabbitmq_queue_*` series on the broker's `/metrics`; publish and ack rates come from `rate()` over `rabbitmq_detailed_queue_exchange_messages_published_total` and `rabbitmq_detailed_queue_messages_acked_total`, which the broker exposes only on `/metrics/detailed`, so publish and ack history needs the deployment's `rabbitmq-detailed` collector scrape job (`/metrics/detailed`, families `queue_delivery_metrics` and `queue_exchange_metrics`). Without it the rate tiles read "No rate data" rather than 0/s. Service availability and response time come from `groovemap_console_poll_duration_seconds{target,outcome}`, endpoint latency from `http_server_request_duration_seconds`. One time-range control drives both history tabs.

The console parses `CACHE_WARMING_ENABLED` and `CACHE_WEBHOOK_SECRET` for configuration
compatibility. Current dashboard code does not open a Valkey client or perform cache warming;
the admin storage panel displays measurements supplied by `catalog-api`.

## Grafana alerts

The attention bar can list firing Grafana alerts. It is off unless both variables are set; with
either missing, `GET /api/alerts` answers `configured: false` and the bar shows only the problems
derived from the live feed.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRAFANA_URL` | unset | Base URL of Grafana, such as `https://grafana.example.net`. |
| `GRAFANA_TOKEN_FILE` | unset | Path to a file holding a Grafana service-account token with read access to alert rules and the Alertmanager API (`alert.instances:read`). Only the file form is read; there is no plain-variable fallback. |

The console asks `{GRAFANA_URL}/api/alertmanager/grafana/api/v2/alerts` (active, not silenced, not
inhibited) from the server, behind the admin session like every other data route, through its own
`GET /api/alerts`. The token is never sent to the browser, and neither it nor an upstream error
message is logged or returned. Alert links point only into the configured Grafana. When Grafana is
configured but does not answer, the token file cannot be read, or the response is not an alert
list, the route answers `available: false` with a note and the bar shows it beside the derived
items.

## HTTP, logging, source, and telemetry

| Variable | Default | Meaning |
| --- | --- | --- |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:8003` | Comma-separated allowed origins. |
| `LOG_LEVEL` | `INFO` | Uvicorn log level. |
| `LOG_FILE_MAX_BYTES` | `104857600` (100 MiB) | Size, in bytes, at which the `/logs/dashboard.log` file sink rolls over. Read by the shared runtime's `setup_logging` at handler-construction time; a non-numeric or non-positive override falls back to the default. |
| `LOG_FILE_BACKUP_COUNT` | `5` | Number of rotated `/logs/dashboard.log` backups the shared runtime retains. Same override rules as `LOG_FILE_MAX_BYTES`. |
| `GROOVEMAP_SOURCE_REVISION` | unset | Full source revision used for the browser-visible corresponding-source link; the image build injects it. |

Telemetry uses the standard OpenTelemetry variables documented in the
[repository README](../README.md#telemetry). When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, the
shared runtime installs no-op telemetry providers.
