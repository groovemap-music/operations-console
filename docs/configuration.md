# Operations-console configuration

This page is for people deploying the console. It lists every environment variable the console reads, with its default and what it does. For a local run, only the datastore variables below are required.

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
| `VICTORIA_METRICS_URL` | `http://victoria-metrics:8428` | Base URL of VictoriaMetrics. The Queue Trends and System Health tabs query `{url}/api/v1/query_range` (and `/api/v1/query` for the endpoint table) through the console's own `/api/history/queues` and `/api/history/health` routes, for the ranges 1h, 6h, 24h, 7d, 30d and 90d; 90d needs the deployment's 90-day retention. The Overview's run progress reads it through `/api/progress/{pipeline}` (instant and range queries at the run's own start and end). The routes are behind the admin session like every other data route. When VictoriaMetrics does not answer they return 503 `history unavailable` and the tabs say so, while the live Overview keeps working. |
| `DISCOGS_EXCHANGE_PREFIX` | `groovemap-discogs` | Prefix promoted from the `discogs-ingestion` catalog-event contract. |
| `MUSICBRAINZ_EXCHANGE_PREFIX` | `groovemap-musicbrainz` | Prefix promoted from the `musicbrainz-ingestion` catalog-event contract. |
| `API_HOST` | `api` | Internal `catalog-api` host used by the constrained admin proxy and to validate admin sessions. |
| `API_PORT` | `8004` | Internal `catalog-api` port. |
| `SESSION_CACHE_SECONDS` | `30` | How long a confirmed admin session is trusted before the console asks `catalog-api` again. Must be a finite number: `nan`, `inf`, or a non-number fails startup. Clamped to 0–30; `0` validates every request. Only positive answers are cached, and never past the session's own expiry. |
| `SESSION_COOKIE_SECURE` | `true` | Whether the admin [session cookie](#admin-session-cookie-and-reverse-proxies) is `Secure`. The default fails closed: the cookie is `Secure` whatever scheme the console itself sees, so a TLS-terminating proxy in front of it needs no setting. Set `false` only when browsers reach the console over plain HTTP on a host other than `localhost`, such as `http://<host>:8003`; the cookie then travels unencrypted. `true`/`1`/`yes` and `false`/`0`/`no` are accepted; any other value fails startup. |

The Platform Health section ([admin guide](admin-guide.md#platform-health)) checks the services on their compose ports; no variable is needed for the defaults. It reuses `RABBITMQ_PROMETHEUS_URL`, `VICTORIA_METRICS_URL` and `GRAFANA_API_URL` (server-side only; `GRAFANA_URL` is browser-facing and is not used), and these optional overrides exist for a deployment that moves a target:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VICTORIA_TRACES_URL` | `http://victoria-traces:10428` | Base URL checked at `/health`. |
| `OTEL_COLLECTOR_HEALTH_URL` | `http://otel-collector:13133` | The collector's `health_check` extension. |
| `VALKEY_HEALTH_ADDRESS` | `valkey:6379` | `host:port` answered with a `PING`. |

The console reads the embeddings job's last load from `public.artist_embeddings` with its existing PostgreSQL connection; its role needs `SELECT` on that table, and without it the job reads "not monitored here".

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
| `GRAFANA_URL` | unset | Browser-facing base URL of Grafana, such as `https://grafana.example.net`. Alert links and the deep links use it. |
| `GRAFANA_API_URL` | `GRAFANA_URL` (the Platform Health Grafana check defaults to `http://grafana:3000` instead) | Base URL the console server uses to reach Grafana's Alertmanager API, such as `http://grafana:3000` on the compose network. Set it when the server and the browser reach Grafana at different addresses. |
| `GRAFANA_TOKEN_FILE` | unset | Path to a file holding a Grafana service-account token with read access to alert rules and the Alertmanager API (`alert.instances:read`). Only the file form is read; there is no plain-variable fallback. |

The console asks `{GRAFANA_API_URL}/api/alertmanager/grafana/api/v2/alerts` (active, not silenced, not
inhibited) from the server, behind the admin session like every other data route, through its own
`GET /api/alerts`. The token is never sent to the browser, and neither it nor an upstream error
message is logged or returned. Alert links point only into `GRAFANA_URL`. Alerts are configured
only when a Grafana URL is set and `GRAFANA_TOKEN_FILE` names a readable, non-empty token; an
unset variable, a missing file, or an empty file answers `configured: false` and the bar says nothing about
Grafana. A token file that exists but cannot be read (permissions, a directory in its place) or is
not valid UTF-8 is a broken mount: the route answers `available: false` with the note and logs the
error class only. When Grafana is configured but does not answer, refuses the token (401 or 403), or the
response is not an alert list, the route answers `available: false` with a note and the bar shows
it beside the derived items.

## Grafana and trace links

Each service, queue, and datastore links into the provisioned Grafana dashboards instead of
redrawing their charts. The links need no token: they are plain URLs the operator's browser opens
in a new tab (`rel="noopener noreferrer"`), so they work with `GRAFANA_URL` alone (`GRAFANA_API_URL` is server-side only and never reaches the browser).

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRAFANA_URL` | unset | Base URL of Grafana. With it unset, no Grafana or trace link is drawn. |
| `TRACES_URL` | `GRAFANA_URL` | Optional base URL of the Grafana that hosts the `groovemap-traces` dashboard, for a deployment that serves traces from a different Grafana. VictoriaTraces itself serves only the Tempo query API, which Grafana reads, so trace links are a Grafana dashboard and not a separate trace UI. |

A base that is not an `http` or `https` URL is treated as unset. The console serves the bases to
the signed-in browser from `GET /api/links`, behind the admin session; the browser adds the
dashboard variables and the current range. Every value is URL-encoded, and the link carries the
shared time range as `from=now-<range>&to=now` (24h when none is chosen).

| Where | Link | Dashboard and variables |
| --- | --- | --- |
| Queue detail drawer | Queue | `groovemap-infrastructure`, `var-queue` = the queue |
| Queue detail drawer | Dead-letter queue | `groovemap-infrastructure`, `var-queue` = the `.dlq` queue |
| Queue detail drawer | Pipeline | `groovemap-pipeline-overview`, `var-source` = `discogs` or `musicbrainz` |
| Queue detail drawer, consumer heading | Consumer | `groovemap-consumers`, `var-service` = the consumer |
| Extractor heading | Grafana | `groovemap-ingestion`, `var-service` = the extractor, `var-source` = the pipeline |
| Extractor and consumer headings, drawer | Traces | `groovemap-traces`, `var-service` = the service |
| Neo4j card | Grafana | `groovemap-neo4j` (all services) |
| PostgreSQL card | Grafana | `groovemap-infrastructure`: PostgreSQL has no dashboard of its own, its panels sit beside RabbitMQ and Valkey |
| Attention item for a queue or a service | the matching links above | as above |

## Admin session cookie and reverse proxies

The browser never holds the admin bearer where page script can read it. When you sign in, the
console takes the bearer `catalog-api` issues and sets it as a cookie. Page script cannot read
the cookie, and the browser sends it only to the console itself:

- `HttpOnly`, so neither `document.cookie` nor any other script can read it;
- `Secure`, so the browser sends it only over HTTPS (or to `http://localhost`, which Chromium
  and Firefox treat as secure; WebKit does not);
- `SameSite=Strict`, so the browser does not send it on a request that starts on another site;
- `Path=/` and host-only (no `Domain`);
- `Max-Age` set to the session's lifetime from `catalog-api`, so the browser drops it when the
  session expires.

The cookie is named `__Host-groovemap_console_session`. Browsers accept that name only on a
`Secure`, host-only, `Path=/` cookie, so a sibling subdomain or a plain-HTTP response cannot
plant one, and the console reads no other name. Only with `SESSION_COOKIE_SECURE=false` is the
cookie plain `groovemap_console_session`, without `Secure`.

The console reads the bearer back from the cookie for its data routes, `POST /api/ws-ticket`,
and the admin proxy, which sends it to `catalog-api` as `Authorization: Bearer`. An explicit
`Authorization` header still works for clients that are not a browser, and it takes precedence:
a request that sends one is never also authenticated by the cookie.

A browser attaches the cookie by itself, so a state-changing request (`POST`, `PUT`, `PATCH`,
`DELETE`) authenticated by the cookie must come from the console's own origin and carry
`X-GrooveMap-Console: 1`. Otherwise the console refuses it with `403` before it asks
`catalog-api` anything. The origin check reads `Sec-Fetch-Site`, which must be `same-origin`
(`same-site` is refused: another port on the same host, such as Grafana on `localhost:3000`, is
the same site); a browser too old to send it is checked by `Origin`, whose host must be the
`Host` the request was sent to. A cross-site form cannot set the custom header, and no other
origin, not even one in `CORS_ORIGINS`, passes the origin check. Login and logout are covered
too. A request with its own `Authorization` header is exempt, because no other page can attach
one. The WebSocket never accepts the cookie: it authenticates only with a ticket from
`POST /api/ws-ticket`.

The console clears the cookie when you sign out, whatever `catalog-api` answers. It also clears
it when `catalog-api` calls the session invalid: a `401` from the session gate, or a `401` from
`catalog-api` behind the proxy. When `catalog-api` cannot answer (`503`), the cookie is kept, so
a reload resumes the session once `catalog-api` is back.

Behind a reverse proxy:

- **HTTPS.** A proxy that terminates TLS needs no console setting: the cookie is `Secure` by
  default. Reaching the console over plain HTTP on any host but `localhost` needs
  `SESSION_COOKIE_SECURE=false`. Without it the browser drops the cookie, so sign-in seems to
  work, then the login form comes straight back.
- **Headers.** Pass `Cookie` and `Set-Cookie` through unchanged: do not rename the cookie or
  rewrite its path or attributes. Also pass the `X-GrooveMap-Console`, `Sec-Fetch-Site`,
  `Origin` and `Authorization` request headers, and keep the browser's `Host` (or the origin
  fallback refuses older browsers). A proxy that strips `X-GrooveMap-Console` or
  `Sec-Fetch-Site` makes every sign-in, sign-out, trigger, and purge answer `403`.
- **Path.** Serve the console at the root of its own host name. The cookie is set on `Path=/`,
  and the page calls absolute `/api` and `/admin/api` paths, so a path prefix is not supported.
- **WebSocket.** Forward `/ws` with its `Upgrade` headers, and do not log its query string (see
  the [admin guide](admin-guide.md#signing-in-to-the-console)).

## HTTP, logging, source, and telemetry

| Variable | Default | Meaning |
| --- | --- | --- |
| `CORS_ORIGINS` | unset (no CORS) | Comma-separated origins allowed read-only, cross-origin access: `GET` with an `Authorization` header only, never credentials (the session cookie) or the `X-GrooveMap-Console` header. The console's own pages are same-origin and need none of it. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | uvicorn's own setting: the proxy addresses whose `X-Forwarded-Proto` and `X-Forwarded-For` it trusts, which the admin proxy forwards to `catalog-api`. |
| `LOG_LEVEL` | `INFO` | Uvicorn log level. |
| `LOG_FILE_MAX_BYTES` | `104857600` (100 MiB) | Size, in bytes, at which the `/logs/dashboard.log` file sink rolls over. Read by the shared runtime's `setup_logging` at handler-construction time; a non-numeric or non-positive override falls back to the default. |
| `LOG_FILE_BACKUP_COUNT` | `5` | Number of rotated `/logs/dashboard.log` backups the shared runtime retains. Same override rules as `LOG_FILE_MAX_BYTES`. |
| `GROOVEMAP_SOURCE_REVISION` | unset | Full source revision used for the browser-visible corresponding-source link; the image build injects it. |

Telemetry uses the standard OpenTelemetry variables documented in the
[telemetry page](telemetry.md). When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, the
shared runtime installs no-op telemetry providers.
