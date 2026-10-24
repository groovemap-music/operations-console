# Operations-console administrator guide

## Creating an admin account

Admin accounts are created via the `admin-setup` CLI tool inside the API container:

```bash
docker exec -it groovemap-api admin-setup --email admin@example.com
```

`admin-setup` never accepts the password as a CLI argument (command-line
arguments are world-readable via `/proc/*/cmdline` and land in shell
history). The command above prompts interactively (input hidden, not
echoed). For scripted/non-interactive use, set `ADMIN_PASSWORD` (or
`ADMIN_PASSWORD_FILE`, pointing at a Docker secret) in the container's
environment instead.

Passwords must be at least 8 characters. If the email already exists, the password is updated.

## Listing admin accounts

```bash
docker exec -it groovemap-api admin-setup --list
```

## Signing in to the console

Navigate to `http://<host>:8003` and sign in with your admin credentials. The console is one
page: until `catalog-api` confirms an admin session it shows only the login form, and after
sign-in the live **Overview** tab comes first, followed by the admin tabs. The old
`http://<host>:8003/admin` and `/admin.html` addresses redirect to `/`.

Every data route (`/api/metrics`, `/api/services`, `/api/queues`, `/api/databases`,
`/api/queue-prefixes`, `/api/session`, `POST /api/ws-ticket`), every admin proxy route except
login and logout, and the live WebSocket require a live admin session. Only `/health`, the
login page and its static assets, the `/admin` redirects, and the login and logout proxy routes
(which `catalog-api` validates itself) are public; the console serves no `/docs`, `/redoc`, or
`/openapi.json`. The console holds no JWT secret:
it asks `catalog-api` (`GET /api/admin/auth/session`) whether the bearer is live and caches a
yes for at most 30 seconds. A 401 or 403 from `catalog-api` (missing, expired, revoked,
password-changed, or non-admin token) is answered `401` with `"reason": "invalid"`; a
`catalog-api` error or an unreachable `catalog-api` is answered `503` with
`"reason": "unavailable"`.

An `invalid` answer for a bearer is reused for 10 seconds, so the same dead bearer is not
re-checked on every request; an `unavailable` answer is never reused. Every check goes through
one shared HTTP client whose pool is capped at 16 connections, so concurrent checks queue for a
connection (within the 5-second timeout) instead of opening unbounded sockets to `catalog-api`.
No client is ever locked out by address.

The console returns to the login form, with the reason shown above it, when you sign out, when
the session expires or is revoked while the console is open, and when `catalog-api` cannot be
reached to confirm it. The open console re-checks its session every 15 seconds and just after
the session's expiry. Signing out always reaches `catalog-api`, even when the console cannot
confirm the session itself, and drops the console's cached copy of the session and every
outstanding WebSocket ticket for it whatever `catalog-api` answers. The login form says
"You signed out." only when `catalog-api` answered 2xx (revoked) or 401 (already dead); any
other answer, or none, is reported as an unconfirmed sign-out, because the session may still be
valid until it expires. The browser forgets the bearer either way. A session revoked elsewhere
stops working for HTTP requests within the 30-second cache window.

The live Overview cannot send `Authorization` on a WebSocket, so it first posts the bearer to
`POST /api/ws-ticket` for a single-use ticket valid for 30 seconds and opens
`/ws?ticket=<ticket>`. Opening the socket re-validates the ticket's bearer with `catalog-api`,
bypassing the cache. A missing, reused, expired, or logged-out ticket is closed with code
`4401`, as is an open socket whose session stops being valid: the socket asks `catalog-api`
again, bypassing the cache, on a fixed 30-second schedule and at the session's expiry, so a
revocation closes it within 30 seconds. When `catalog-api` cannot answer for the socket's
session, it is closed with `4503` instead; the browser keeps the session and retries the socket
after 5 seconds, doubling the wait up to 60 seconds and resetting it once a socket delivers data.
Every reconnect waits on that backoff, so a refused socket never turns into a reconnect loop.
The console never expects frames from the browser; any it receives are discarded and cannot
delay a re-check. The
console removes the ticket from the request's query string before its access log or request
spans read it and redacts it from server log lines, but a reverse proxy in front of the console
keeps its own access log: configure it not to log query strings for `/ws`.

## Reading the Overview

The Overview draws one matrix per pipeline from `/api/topology`: rows are entity types, columns
are consumers with the datastore they write to, and each cell is one work queue. A cell shows the
messages queued, an ↑ or ↓ when the depth changed by 1,000 or more over the last 5 minutes, the
lag, and a badge for its dead-letter queue. Lag is the backlog divided by the queue's mean ack
rate over the last 5 minutes, and it colours the cell in four bins (under 1 minute, under 10
minutes, under 1 hour, 1 hour or more); the legend above the matrices names them.

Every cell is judged by the same rules, from the pipeline-visualization spike
([gm-operations-console-2rf](spikes/gm-operations-console-2rf-pipeline-visualization.md)):

| State | Rule | Shown as |
|---|---|---|
| Stalled (critical) | Messages queued, at least one consumer, and a computed ack rate of 0 for at least 60 seconds | ◆ `STALLED`, thick border, hatched cell |
| No consumer (critical) | Messages queued and no consumer | ◆ `NO CONSUMER`, thick border, hatched cell |
| DLQ growing (critical) | The dead-letter queue is deeper than 5 minutes ago | Filled ◆ DLQ badge with the growth; the cell is hatched |
| DLQ not empty (warning) | The dead-letter queue holds messages and is not growing | ▲ DLQ badge with the depth |

A queue filling during an extraction is not an alarm. A rate the console cannot compute (the
first reading, a missed poll, a broker counter reset) is shown as unknown and never counts toward
a stall, so a stall needs 60 seconds of real zero-ack readings. The browser keeps the last 5
minutes of readings for these rules; it starts them again when the live feed reconnects or a
pipeline's queue metrics become unavailable, which a pipeline then shows with a banner and every
cell marked unavailable. Each change into or out of a critical or warning state is written to the
System Event Log of the browser that saw it.

The System Event Log also lists what the console server observed, the same for every operator:
a service going healthy or unhealthy, a datastore going up or down, queue metrics becoming
unavailable or returning, and a dead-letter queue growing or emptying. The server keeps the most
recent 200 of these, and a browser that connects or reconnects receives them at once. Recent
extraction triggers and DLQ purges, with the operator who did them, are merged in from the audit
log. Connecting and disconnecting are shown by the connection indicator only.

Select a cell (or focus it and press Enter) to open the queue's details: its depth, ready and
unacknowledged messages, consumers, publish and ack rates, the 5-minute mean ack rate, lag,
backlog change, how long acks have been at zero, and its dead-letter queue. Escape closes it.
Below 480 px wide, a pipeline with more than two consumers lists each entity's cells in two
columns, and every cell names its consumer.

## Run progress

While an extraction runs, a headline above its pipeline's matrix says when it will finish:
"Finishes in 25m, bottleneck Graphinator" names the time until the slowest consumer has loaded
every expected record, and that consumer. "◆ Cannot finish: Tableinator · Releases stalled" lists
every queue the matrix marks stalled, without a consumer, or with a growing DLQ. With no
extraction running there is no headline.

**Run progress**, next to **Matrix** above the pipelines, is the secondary view of the same run:
per pipeline, a burn-up of the loaded fraction per consumer since the run started, dashed to 100 %
at each consumer's ETA, and per entity a bar per consumer of the records loaded (solid) against
those extracted but still queued (light). **Run details** opens the run's row in the Extractions
tab. A finished run shows its duration, final counts, and what each consumer loaded.

The numbers come from VictoriaMetrics through `/api/progress/{pipeline}`, for the run's start and
end from the extraction history:

| Number | Source |
|---|---|
| Expected records per entity | Records extracted in the run (`groovemap_extraction_records_total`) divided by the fraction of the file read (`groovemap_extraction_file_progress_ratio`) |
| Loaded per queue | `rabbitmq_detailed_queue_messages_acked_total` minus its value at the run's start, corrected for counter resets |
| ETA | Records still to load (expected, less loaded and dead-lettered) over the queue's 5-minute mean ack rate |

Nothing is kept in the console: the baseline is read at the run's start time on every request,
so a console restart mid-run changes nothing. When a total or a baseline cannot be derived (no
file-progress sample in the run, an entity not extracted yet, no data from the run's start) the
view says "progress unavailable" and why, and the headline says so too; it never shows a guessed
total. An unknown or zero ack rate gives no ETA ("Finish time unknown"). Extraction records do not
name their source, and catalog-api's trigger reaches the Discogs extractor, so runs are shown
against the Discogs pipeline.

## The attention bar and the header status

Under the header, on every tab, the attention bar lists everything that needs an operator and is
hidden while nothing does. It shows a count and every critical (◆) and warning (▲) item, critical
first, and **Hide** collapses the list until a new item appears. Each item opens what it is about:
a queue item opens that matrix cell's detail drawer, a service item scrolls to its node, a
datastore item opens the Storage tab, and a Grafana alert opens in Grafana.

| Item | Severity |
|---|---|
| A queue stalled, or with a backlog and no consumer; a dead-letter queue that is growing | Critical |
| A dead-letter queue that holds messages and is not growing | Warning |
| An unhealthy service, or an unavailable datastore | Critical |
| A service whose status is unknown or starting; a pipeline whose queue metrics are unavailable | Warning |
| A firing Grafana alert | Critical when labelled `severity=critical`, otherwise warning |

Queue items use the matrix's own rules above, so the bar and the matrix cannot disagree. Grafana
alerts appear only when `GRAFANA_URL` and `GRAFANA_TOKEN_FILE` are configured
([configuration](configuration.md#grafana-alerts)); if Grafana does not answer, the bar keeps the
items derived from the live feed and says "Grafana alerts unavailable".

The header status (Operational, Warning, Critical, Offline) is computed from the same items. It
reads Unknown only before the console has reported any pipeline or datastore, and Connecting until
the first reading.

## Triggering an extraction

Use **Trigger Discogs Extraction** or **Trigger MusicBrainz Extraction** in the admin panel.
The console sends these requests through fixed proxy routes; `catalog-api` owns authorization
and dispatches the trigger to the selected source-owned producer.

| Source | Console route | Producer | Published exchanges |
| --- | --- | --- | --- |
| Discogs | `POST /admin/api/extractions/trigger` | `discogs-ingestion` | `groovemap-discogs-{artists,labels,masters,releases}` |
| MusicBrainz | `POST /admin/api/extractions/trigger-musicbrainz` | `musicbrainz-ingestion` | `groovemap-musicbrainz-{artists,labels,release-groups,releases}` |

Both console routes map to the promoted `catalog-api` operation
`POST /api/admin/extractions/trigger`; the MusicBrainz adapter adds
`"source":"musicbrainz"` to the validated JSON object. Each ingestion service owns its own
download, state, normalization, and publication lifecycle. There is no combined ingestion
runtime or shared producer lock.

Use this when:

- A previous extraction failed and you want to retry
- You suspect data corruption and want a clean reprocess
- A new source dump has been published and you do not want to wait for its producer's periodic check

The extraction runs asynchronously. Progress is tracked in the extraction history table.

If an extraction is already running, the trigger returns an error — wait for it to complete first.

## DLQ management

Dead-letter queues (DLQs) collect messages that consumers failed to process. Each data type has a DLQ per consumer:

| Queue                                                             | Consumer          |
| ----------------------------------------------------------------- | ----------------- |
| `groovemap-discogs-graphinator-artists.dlq`                  | `discogs-graph-enricher`       |
| `groovemap-discogs-graphinator-labels.dlq`                   | `discogs-graph-enricher`       |
| `groovemap-discogs-graphinator-masters.dlq`                  | `discogs-graph-enricher`       |
| `groovemap-discogs-graphinator-releases.dlq`                 | `discogs-graph-enricher`       |
| `groovemap-discogs-tableinator-artists.dlq`                  | `discogs-sql-loader`           |
| `groovemap-discogs-tableinator-labels.dlq`                   | `discogs-sql-loader`           |
| `groovemap-discogs-tableinator-masters.dlq`                  | `discogs-sql-loader`           |
| `groovemap-discogs-tableinator-releases.dlq`                 | `discogs-sql-loader`           |
| `groovemap-musicbrainz-brainzgraphinator-artists.dlq`        | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainzgraphinator-labels.dlq`         | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainzgraphinator-release-groups.dlq` | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainzgraphinator-releases.dlq`       | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainztableinator-artists.dlq`        | `musicbrainz-sql-loader`       |
| `groovemap-musicbrainz-brainztableinator-labels.dlq`         | `musicbrainz-sql-loader`       |
| `groovemap-musicbrainz-brainztableinator-release-groups.dlq` | `musicbrainz-sql-loader`       |
| `groovemap-musicbrainz-brainztableinator-releases.dlq`       | `musicbrainz-sql-loader`       |

**Purging** permanently deletes all messages in a DLQ. Do this when:

- Messages are known-bad and will never succeed on retry
- After fixing the root cause and retriggering an extraction

Purging cannot be undone.

DLQ names follow the promoted producer-contract pattern
`{exchange-prefix}-{consumer}-{entity}.dlq`. The consumer keys in the queue names
(`graphinator`, `tableinator`, `brainzgraphinator`, and `brainztableinator`) are stable wire
identifiers even though the owning repositories have descriptive names. The optional
`DISCOGS_EXCHANGE_PREFIX` and `MUSICBRAINZ_EXCHANGE_PREFIX` overrides are documented in
[configuration](configuration.md).

## Metrics history and trend analysis

### Queue and health history endpoints

The browser calls the console routes below. The console forwards them to the corresponding
fixed paths in the promoted `catalog-api` contract.

| Data | Console route | Upstream `catalog-api` route |
| --- | --- | --- |
| Queue history | `GET /admin/api/queues/history?range=<range>` | `GET /api/admin/queues/history?range=<range>` |
| Service health | `GET /admin/api/health/history?range=<range>` | `GET /api/admin/health/history?range=<range>` |

Both endpoints require admin authentication (Bearer token).

**Valid range values:**

| Range  | Description             | Data Granularity  |
| ------ | ----------------------- | ----------------- |
| `1h`   | Last 1 hour             | 5-minute buckets  |
| `6h`   | Last 6 hours            | 5-minute buckets  |
| `24h`  | Last 24 hours (default) | 15-minute buckets |
| `7d`   | Last 7 days             | 1-hour buckets    |
| `30d`  | Last 30 days            | 6-hour buckets    |
| `90d`  | Last 90 days            | 1-day buckets     |
| `365d` | Last 365 days           | 1-day buckets     |

Granularity is selected automatically based on the requested range. Omitting the `range` parameter defaults to `24h`.

### Historical metrics collector

A background collector runs inside `catalog-api`, not operations-console, and periodically
samples queue depths and service health. It stores history in PostgreSQL for these queries.

The collector interval is controlled by the `METRICS_COLLECTION_INTERVAL` environment variable (default: 300 seconds / 5 minutes).

### Collector environment variables

| Variable                      | Default | Description                                                                              |
| ----------------------------- | ------- | ---------------------------------------------------------------------------------------- |
| `METRICS_RETENTION_DAYS`      | `366`   | How many days of metrics to retain in the database. Older rows are pruned automatically. |
| `METRICS_COLLECTION_INTERVAL` | `300`   | Seconds between each metrics collection cycle in the background collector.               |

These variables configure `catalog-api`; they are not operations-console variables. Set them
on the API service through deployment configuration:

```dotenv
METRICS_RETENTION_DAYS=366
METRICS_COLLECTION_INTERVAL=300
```

### Database tables

`database-schema` owns the persistence definitions. `catalog-api` writes and queries two
PostgreSQL tables:

**`queue_metrics`** — RabbitMQ queue depth snapshots:

| Column                    | Type         | Description                                      |
| ------------------------- | ------------ | ------------------------------------------------ |
| `id`                      | bigint       | Primary key (generated always as identity)       |
| `recorded_at`             | timestamptz  | When the sample was taken                        |
| `queue_name`              | varchar(100) | Name of the RabbitMQ queue                       |
| `messages_ready`          | integer      | Number of ready messages at sample time          |
| `messages_unacknowledged` | integer      | Number of unacknowledged messages at sample time |
| `consumers`               | integer      | Number of active consumers at sample time        |
| `publish_rate`            | real         | Message publish rate                             |
| `ack_rate`                | real         | Message acknowledgement rate                     |

**`service_health_metrics`** — Per-service health check results:

| Column             | Type        | Description                                             |
| ------------------ | ----------- | ------------------------------------------------------- |
| `id`               | bigint      | Primary key (generated always as identity)              |
| `recorded_at`      | timestamptz | When the sample was taken                               |
| `service_name`     | varchar(50) | Runtime service identity (for example, the aliases for `discogs-graph-enricher` and `discogs-sql-loader`) |
| `status`           | varchar(20) | Health status (`healthy`, `unhealthy`, `unknown`)       |
| `response_time_ms` | real        | Health check response time in milliseconds              |
| `endpoint_stats`   | jsonb       | Per-endpoint latency statistics (API service only)      |

Both tables are indexed on `recorded_at` for efficient range queries. Rows older than `METRICS_RETENTION_DAYS` are pruned automatically.

### Queue Trends and System Health tabs

The console (`http://<host>:8003`) exposes two tabs backed by the history endpoints:

- **Queue Trends** — Line charts showing message depth over time for each RabbitMQ queue. Use the range selector (1h / 6h / 24h / 7d / 30d / 90d / 365d) to zoom in or out.
- **System Health** — Status timeline showing per-service health over the selected range. Unhealthy periods are highlighted in red; response time is shown as a secondary series.

## Extraction Analysis: Media Mapping Coverage

The **Extraction Analysis** tab's single-version report includes a **Media Mapping Coverage**
panel, alongside the existing pipeline status, skipped records, and violations-by-entity
cards. It shows how many of a provider's releases carry a media name the canonical taxonomy
doesn't recognize — the signal that the taxonomy needs a new entry. Both providers are
covered: selecting a MusicBrainz version gives a real reading, not a "not observable" note.

For the provider behind the selected extraction version, the panel shows:

- **Provider** — `discogs` or `musicbrainz`, resolved from the selected version's `source`.
- **Releases with unmapped media** — releases whose stored `media` block carries at least
  one unmapped name, either a provider format name or a description qualifier.
- **Media-tagged releases** — the denominator: every release whose `media` column is
  populated.
- **Distinct unmapped names** and a table of the **top unmapped names** with a **kind**
  (`format` or `description`) and a per-name release count, so an operator can see at a
  glance which raw strings are missing from the taxonomy.
- **Unmapped rate** — unmapped releases as a share of media-tagged releases.

**Where the data comes from.** catalog-api exposes
`GET /api/admin/media/unmapped?provider=…&limit=…` (contract operation
`admin_unmapped_media`), which ranks the raw names each loader kept under
`releases.media -> 'unmapped'` when the [ADR 0007](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md)
canonical taxonomy did not recognize them. The console's
`/admin/api/extraction-analysis/{version}/media-mapping-coverage` route calls the existing
`/summary` route only to learn which provider the selected version belongs to, then reads
every number from that upstream route. The ranked list is capped at 10 names; when more
distinct names exist upstream the panel shows a **truncated** notice under the table. The
counts themselves are always exact.

**What changed from the earlier reading.** This panel previously counted discogs-ingestion
`format-not-recognized` data-quality violations for one extraction version. The Discogs
numbers moved when it switched to the route above, and the two readings are not expected to
agree:

- It is a **release** count, not an occurrence count. The taxonomy de-duplicates each
  release's `unmapped` list, so a release naming one unrecognized format twice contributes
  1 where the rules engine could emit two violations.
- It covers **description qualifiers** as well as format names; the old rule only ever fired
  on `formats.format.@name`.
- It is **table-wide, not per-version**. The selected version chooses the provider and
  nothing more, so the reading reflects everything loaded for that provider rather than one
  extraction run.
- The denominator is now **media-tagged releases** rather than the count of release-entity
  violations of any rule, so the rate is a share of real releases rather than a share of
  flagged ones.

Both tabs auto-refresh every 60 seconds and respect the currently selected time range.
