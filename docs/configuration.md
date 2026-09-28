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
| `RABBITMQ_HOST` | `rabbitmq` | AMQP host and fallback management-API host. |
| `RABBITMQ_PORT` | `5672` | AMQP port. |
| `RABBITMQ_USERNAME` / `RABBITMQ_PASSWORD` | `groovemap` | AMQP and management credentials; `_FILE` variants are supported. |
| `RABBITMQ_MANAGEMENT_HOST` | `RABBITMQ_HOST` | RabbitMQ management-API host. |
| `RABBITMQ_MANAGEMENT_PORT` | `15672` | RabbitMQ management-API port. |
| `DISCOGS_EXCHANGE_PREFIX` | `groovemap-discogs` | Prefix promoted from the `discogs-ingestion` catalog-event contract. |
| `MUSICBRAINZ_EXCHANGE_PREFIX` | `groovemap-musicbrainz` | Prefix promoted from the `musicbrainz-ingestion` catalog-event contract. |
| `API_HOST` | `api` | Internal `catalog-api` host used only by the constrained admin proxy. |
| `API_PORT` | `8004` | Internal `catalog-api` port. |

The console also parses `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`,
`CACHE_WARMING_ENABLED`, and `CACHE_WEBHOOK_SECRET` for configuration compatibility. Current
dashboard code does not open a Redis client or perform cache warming.

## HTTP, logging, source, and telemetry

| Variable | Default | Meaning |
| --- | --- | --- |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:8003` | Comma-separated allowed origins. |
| `LOG_LEVEL` | `INFO` | Uvicorn log level. |
| `GROOVEMAP_SOURCE_REVISION` | unset | Full source revision used for the browser-visible corresponding-source link; the image build injects it. |

Telemetry uses the standard OpenTelemetry variables documented in the
[repository README](../README.md#telemetry). When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, the
shared runtime installs no-op telemetry providers.
