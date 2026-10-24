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
| `DISCOGS_EXCHANGE_PREFIX` | `groovemap-discogs` | Prefix promoted from the `discogs-ingestion` catalog-event contract. |
| `MUSICBRAINZ_EXCHANGE_PREFIX` | `groovemap-musicbrainz` | Prefix promoted from the `musicbrainz-ingestion` catalog-event contract. |
| `API_HOST` | `api` | Internal `catalog-api` host used only by the constrained admin proxy. |
| `API_PORT` | `8004` | Internal `catalog-api` port. |

Publish and ack rates are not broker-reported values: the Prometheus plugin exposes cumulative counters, and the console divides the counter delta between consecutive two-second polls by the elapsed time. A queue's first sample, the first sample after it was missing from a poll, and an interval in which a counter went down (a reset) have no computable rate and report `0.0`. If a scrape fails or returns a non-200 status, the console logs a warning and marks that pipeline's queue data unavailable (`queues_available: false` in `/api/metrics` and the WebSocket payload, `null` for the pipeline in `/api/queues`) instead of reporting an empty pipeline.

The console parses `CACHE_WARMING_ENABLED` and `CACHE_WEBHOOK_SECRET` for configuration
compatibility. Current dashboard code does not open a Valkey client or perform cache warming;
the admin storage panel displays measurements supplied by `catalog-api`.

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
